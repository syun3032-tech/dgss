"""申請データの保存・復元の整合テスト（保存ロールバック対策の回帰テスト）。

2026-07 の川野さん報告「編集が巻き戻る」の再発防止:
  1. 保存(apply)は端末の編集時刻 mtime を client_mtime として行に刻む
  2. localStorage 復元(/applications/restore)は、サーバ行の方が新しければ上書きしない
  3. 部分ミラー（案件詳細フォーム由来）の復元で他項目（金額・原価内訳）を消さない
  4. Supabase 往復（_applications_for_supa → restore_from_supa）で
     win_company / cost_items / agency_override / client_mtime が脱落しない

依存: Flask（requirements.txt に含まれる）。DBは一時ファイルを使う。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

# 最重要: 本テストは一時DBに書き込むたび _push_applications が走る。
# CI/ビルド環境に本番の SUPABASE_DB_URL があると、テストデータで本番KVを
# 上書きしてしまうため、db を import する前に必ず無効化する。
os.environ.pop("SUPABASE_DB_URL", None)

import db  # noqa: E402

assert not db.supa.enabled(), "supa must be disabled during tests"

db.DB_PATH = Path(tempfile.mkdtemp()) / "test_apps.db"
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


def setup_case(ext: str, title: str) -> int:
    db.upsert_cases([{
        "source": "kkj", "external_id": ext,
        "title": title, "agency": "テスト市", "category": "電気・照明",
    }])
    case_id = db.get_case_id_by_external(ext)
    assert case_id is not None
    return case_id


# ---------- 1. 保存で client_mtime が刻まれる ----------
cid = setup_case("kkj:sync-1", "テスト照明改修工事")
r = client.post(f"/case/{cid}/apply", data={
    "ajax": "1", "mtime": "2000000",
    "managed": "status,note,bid_plan,win_company,cost_items,agency_override",
    "status": "見積取得", "note": "最新の編集", "bid_plan": "8000000",
    "win_company": "テスト電設", "agency_override": "テスト県",
    "cost_items": '[{"label":"器具","amount":100}]',
})
check("apply が 204", r.status_code == 204)
row = db.get_application(cid) or {}
check("client_mtime が刻まれる", row.get("client_mtime") == 2000000)

# mtime 未送信（旧クライアント）はサーバ時刻で代用され 0 にならない
r = client.post(f"/case/{cid}/apply", data={
    "ajax": "1", "managed": "status,note", "status": "見積取得", "note": "最新の編集",
})
row = db.get_application(cid) or {}
check("mtime未送信でも client_mtime > 0", int(row.get("client_mtime") or 0) > 0)
base_mtime = int(row.get("client_mtime") or 0)

# ---------- 2. 古いミラーは巻き戻さない / 新しいミラーは反映 ----------
r = client.post("/applications/restore", json={"items": [{
    "external_id": "kkj:sync-1", "status": "参加申請準備前",
    "note": "古いメモ", "mtime": 1000000,
}]})
check("古いミラーは restored=0", (r.get_json() or {}).get("restored") == 0)
row = db.get_application(cid) or {}
check("古いミラーで巻き戻らない", row.get("note") == "最新の編集")

r = client.post("/applications/restore", json={"items": [{
    "external_id": "kkj:sync-1", "status": "入札書提出済み",
    "note": "新しい編集", "mtime": base_mtime + 10,
}]})
check("新しいミラーは restored=1", (r.get_json() or {}).get("restored") == 1)
row = db.get_application(cid) or {}
check("新しいミラーは反映", row.get("status") == "入札書提出済み")

# ---------- 3. 部分ミラーで他項目が消えない ----------
check("部分ミラーで bid_plan 温存", row.get("bid_plan") == 8000000)
check("部分ミラーで cost_items 温存",
      row.get("cost_items") == [{"label": "器具", "amount": 100}])
check("部分ミラーで win_company 温存", row.get("win_company") == "テスト電設")

# mtime の無い旧形式ミラーは、行が存在する限り上書きしない
r = client.post("/applications/restore", json={"items": [{
    "external_id": "kkj:sync-1", "status": "NG", "note": "旧形式",
}]})
check("旧形式ミラーは行があれば restored=0", (r.get_json() or {}).get("restored") == 0)

# 行が消えた後（DB差し替え相当）は旧形式でも復元できる
db.delete_application(cid)
r = client.post("/applications/restore", json={"items": [{
    "external_id": "kkj:sync-1", "status": "NG", "note": "旧形式restore",
}]})
check("行消失後は旧形式でも restored=1", (r.get_json() or {}).get("restored") == 1)

# ---------- 4. Supabase 往復で新しめの列が脱落しない ----------
cid2 = setup_case("kkj:sync-2", "テスト空調更新工事")
client.post(f"/case/{cid2}/apply", data={
    "ajax": "1", "mtime": "5000000",
    "managed": "status,bid_plan,win_company,cost_items,agency_override",
    "status": "見積取得", "bid_plan": "3000000",
    "win_company": "往復テスト電気", "agency_override": "往復県",
    "cost_items": '[{"label":"労務","amount":50}]',
})
snapshot = db._applications_for_supa()
snap2 = next(s for s in snapshot if s.get("external_id") == "kkj:sync-2")
check("スナップショットに client_mtime", int(snap2.get("client_mtime") or 0) == 5000000)
check("スナップショットに cost_items", snap2.get("cost_items") == [{"label": "労務", "amount": 50}])

db.delete_application(cid2)  # 再起動でSQLite側が消えた状態を再現


class _FakeSupa:
    """restore_from_supa にスナップショットを食わせるスタブ。"""

    def enabled(self) -> bool:
        return True

    def load(self, key: str) -> Any:
        return snapshot if key == "applications" else None


_real_supa = db.supa
db.supa = _FakeSupa()  # type: ignore[assignment]
try:
    counts = db.restore_from_supa()
finally:
    db.supa = _real_supa
check("Supabase復元が実行される", counts.get("applications", 0) >= 1)
row2 = db.get_application(cid2) or {}
check("往復で win_company 残る", row2.get("win_company") == "往復テスト電気")
check("往復で cost_items 残る", row2.get("cost_items") == [{"label": "労務", "amount": 50}])
check("往復で agency_override 残る", row2.get("agency_override") == "往復県")
check("往復で client_mtime 残る", int(row2.get("client_mtime") or 0) == 5000000)

# ---------- 5. 公共【役務】シートと案件名の後編集（2026-07 要望） ----------
r = client.post("/applications/new", data={
    "title": "庁舎電気設備 保守点検業務", "sector": "公共役務",
    "status": "参加申請準備前",
})
d = r.get_json() or {}
case3 = d.get("case") or {}
check("役務案件を追加できる", case3.get("sector") == "公共役務")
cid3 = case3.get("case_id")

# 手動案件は案件名を後から変更できる
r = client.post(f"/case/{cid3}/apply", data={
    "ajax": "1", "mtime": "6000000", "managed": "status,title",
    "status": "参加申請準備前", "title": "庁舎電気設備 保守点検業務（改題）",
})
check("title付き保存が 204", r.status_code == 204)
row3 = next((a for a in db.list_applications(None) if a.get("case_id") == cid3), {})
check("手動案件の案件名を変更できる", row3.get("title") == "庁舎電気設備 保守点検業務（改題）")

# スクレイプ案件（source=kkj）の案件名は変更されない
r = client.post(f"/case/{cid}/apply", data={
    "ajax": "1", "mtime": "7000000", "managed": "status,title",
    "status": "NG", "title": "改ざんタイトル",
})
rowk = next((a for a in db.list_applications(None) if a.get("case_id") == cid), {})
check("スクレイプ案件の案件名は変わらない", rowk.get("title") == "テスト照明改修工事")

# ---------- 6. NG理由のAI集計エンドポイント（AI呼び出しはスタブ） ----------
client.post(f"/case/{cid}/apply", data={
    "ajax": "1", "mtime": "8000000", "managed": "status,note",
    "status": "NG", "note": "実績がないため参加できない",
})

_real_can_use = appmod.auth.can_use_ai
_real_enabled = appmod.ai_assist.is_enabled
_real_sum = appmod.ai_assist.summarize_ng_reasons
_seen_items: list[Any] = []


def _fake_sum(items):
    _seen_items.extend(items)
    return {"enabled": True, "total": len(items), "model": "stub",
            "categories": [{"reason": "実績不足", "count": len(items),
                            "examples": [items[0]["title"]]}],
            "insight": "実績づくりが有効"}


appmod.auth.can_use_ai = lambda: True
appmod.ai_assist.is_enabled = lambda: True
appmod.ai_assist.summarize_ng_reasons = _fake_sum
try:
    r = client.post("/reports/ng-reasons?sheet=公共")
    d = r.get_json() or {}
    check("NG集計が返る", (d.get("categories") or [{}])[0].get("reason") == "実績不足")
    check("NGメモが渡っている", any("実績がない" in (i.get("note") or "") for i in _seen_items))
    # 同じ内容なら2回目はキャッシュ（AIを呼ばない）
    n_before = len(_seen_items)
    r2 = client.post("/reports/ng-reasons?sheet=公共")
    check("2回目はキャッシュ命中", (r2.get_json() or {}).get("cached") is True and len(_seen_items) == n_before)
finally:
    appmod.auth.can_use_ai = _real_can_use
    appmod.ai_assist.is_enabled = _real_enabled
    appmod.ai_assist.summarize_ng_reasons = _real_sum

print(f"\n{_ok}/{_ok + _ng} passed")
sys.exit(1 if _ng else 0)
