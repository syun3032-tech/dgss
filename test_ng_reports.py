"""NG集計の保存記録（ng_reports）の回帰テスト。

2026-07-22 の川野さん要望「集計を記録に残したい」:
  1. 保存 → 一覧（シート別・新しい順）→ 削除 が一巡すること
  2. /reports/ng-history/save が中身の無いデータを 400 で弾くこと
  3. Supabase 往復（_push_ng_reports → restore_from_supa）で記録が消えないこと
  4. 復元は「空/欠損なら消さない」安全網が効くこと

依存: Flask（requirements.txt に含まれる）。DBは一時ファイルを使う。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

# 最重要: 本テストは書き込みのたび _push_ng_reports が走る。CI/ビルド環境に
# 本番の SUPABASE_DB_URL があると本番KVを汚すため、import 前に必ず無効化する。
os.environ.pop("SUPABASE_DB_URL", None)

import db  # noqa: E402

assert not db.supa.enabled(), "supa must be disabled during tests"

db.DB_PATH = Path(tempfile.mkdtemp()) / "test_ng_reports.db"
db.init_db()

import app as appmod  # noqa: E402  (DB差し替え後にimportする)

client = appmod.app.test_client()

_ok = 0
_ng = 0


def check(name: str, cond: bool) -> None:
    global _ok, _ng
    if cond:
        _ok += 1
        print(f"  ok  {name}")
    else:
        _ng += 1
        print(f"  NG  {name}")


SAMPLE = {"categories": [{"reason": "地域要件", "count": 2,
                          "breakdown": [{"item": "北海道", "count": 2}]}],
          "insight": "テスト", "total": 2}

print("[1] 保存 → 一覧 → 削除")
db.add_ng_report("公共", json.dumps(SAMPLE, ensure_ascii=False))
db.add_ng_report("民間", json.dumps(SAMPLE, ensure_ascii=False))
rows = db.list_ng_reports("公共")
check("公共シートの記録が1件", len(rows) == 1 and rows[0]["sheet"] == "公共")
check("payloadが往復できる", json.loads(rows[0]["payload"])["total"] == 2)
check("全シートだと2件", len(db.list_ng_reports()) == 2)
db.delete_ng_report(rows[0]["id"])
check("削除後は公共0件・民間1件",
      not db.list_ng_reports("公共") and len(db.list_ng_reports("民間")) == 1)

print("[2] エンドポイント")
res = client.post("/reports/ng-history/save",
                  json={"sheet": "公共", "data": SAMPLE})
check("saveが200/ok", res.status_code == 200 and res.get_json().get("ok"))
res = client.get("/reports/ng-history?sheet=%E5%85%AC%E5%85%B1")
reports = res.get_json().get("reports") or []
check("historyに保存が出る", len(reports) == 1
      and reports[0]["data"]["categories"][0]["reason"] == "地域要件")
res = client.post("/reports/ng-history/save", json={"sheet": "公共", "data": {}})
check("空データは400", res.status_code == 400)
res = client.post(f"/reports/ng-history/{reports[0]['id']}/delete")
check("deleteが200", res.status_code == 200)
check("削除がhistoryに反映",
      not (client.get("/reports/ng-history?sheet=%E5%85%AC%E5%85%B1")
           .get_json().get("reports")))

print("[3] Supabase 往復（スタブ）")
db.add_ng_report("公共", json.dumps(SAMPLE, ensure_ascii=False))
snapshot = db.list_ng_reports()


class _FakeSupa:
    """restore_from_supa にスナップショットを食わせるスタブ。"""

    def enabled(self) -> bool:
        return True

    def load(self, key: str) -> Any:
        return snapshot if key == "ng_reports" else None

    def save(self, key: str, obj: Any) -> bool:
        return True


with db._connect() as conn:  # ローカルの記録を消してから復元できるか
    conn.execute("DELETE FROM ng_reports")
    conn.commit()
_real_supa = db.supa
db.supa = _FakeSupa()  # type: ignore[assignment]
try:
    counts = db.restore_from_supa()
finally:
    db.supa = _real_supa
restored = db.list_ng_reports("公共")
# snapshot には [1] の民間1件も含まれるため、復元件数は snapshot 全体と比較する
check("復元で記録が戻る",
      counts.get("ng_reports") == len(snapshot) and len(restored) == 1)
check("created_at/sheetが維持される",
      restored[0]["created_at"] == snapshot[0]["created_at"]
      and restored[0]["sheet"] == "公共")

print("[4] 空リストでは消さない安全網")
snapshot = []
db.supa = _FakeSupa()  # type: ignore[assignment]
try:
    db.restore_from_supa()
finally:
    db.supa = _real_supa
check("空復元でも既存記録が残る", len(db.list_ng_reports("公共")) == 1)

print(f"\npassed {_ok} / failed {_ng}")
raise SystemExit(1 if _ng else 0)
