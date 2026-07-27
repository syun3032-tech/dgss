"""案件の重複統合（dedupe_cases）と AI 使用量集計（ai_usage）の回帰テスト。

2026-07-27 の要望:
  A. 取得元をまたいだ同一案件の重複を突合して綺麗にする
     1. 同名・同機関・公告日近接の再掲は1件に統合し、締切/URL等を引き継ぐ
     2. 締切が食い違う同名案件（別ロット）は統合しない
     3. 公告日が大きく離れた同名案件（年度をまたぐ再調達）は統合しない
     4. 申請(applications)つきの行は絶対に消えない
     5. 落札実績行の winner が公告行へ引き継がれる
  B. AI使用量カウンター（従量請求の根拠）
     6. add_ai_usage が月×機能×モデルで加算される
     7. Supabase 往復（restore_from_supa）で集計が消えない・空では消さない

依存: 標準ライブラリのみ。DBは一時ファイルを使う。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

# CI/ビルド環境の本番 SUPABASE_DB_URL で本番KVを汚さないよう import 前に無効化。
os.environ.pop("SUPABASE_DB_URL", None)

import db  # noqa: E402

assert not db.supa.enabled(), "supa must be disabled during tests"

db.DB_PATH = Path(tempfile.mkdtemp()) / "test_dedupe.db"
db.init_db()

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


def case(ext: str, **kw: Any) -> dict:
    base = {"source": "官公需API", "external_id": ext,
            "title": "庁舎電気設備改修工事", "agency": "テスト市"}
    base.update(kw)
    return base


print("[1] 同名・同機関・公告日近接の再掲 → 1件に統合＋情報引き継ぎ")
db.upsert_cases([
    case("A1", announced_date="2026-06-01", deadline="",
         detail_url="", description="再掲その1"),
    case("A2", announced_date="2026-06-10", deadline="2026-07-01",
         detail_url="https://example.com/a", spec_url="https://example.com/spec.pdf",
         spec_status="available"),
    case("A3", title="庁舎　電気設備改修工事（108KByte）",  # 空白と添付サイズ表記の揺れ
         announced_date="2026-06-15", deadline=""),
])
n = db.dedupe_cases()
rows = [r for r in db.list_cases(q="庁舎電気設備改修工事")]
check("2件が統合されて1件になる", n == 2 and len(rows) == 1)
keeper = rows[0]
check("詳細URL・締切のある行が残る",
      keeper["detail_url"] == "https://example.com/a"
      and keeper["deadline"] == "2026-07-01")
check("消えた行の説明が引き継がれる", keeper["description"] == "再掲その1")

print("[2] 締切が食い違う同名案件は統合しない")
db.upsert_cases([
    case("B1", title="保安管理業務委託", announced_date="2026-06-01",
         deadline="2026-06-20"),
    case("B2", title="保安管理業務委託", announced_date="2026-06-01",
         deadline="2026-07-20"),
])
db.dedupe_cases()
check("別ロット2件が残る", len(db.list_cases(q="保安管理業務委託")) == 2)

print("[3] 公告日が150日超離れた同名案件は統合しない")
db.upsert_cases([
    case("C1", title="構内配電設備点検", announced_date="2025-05-01"),
    case("C2", title="構内配電設備点検", announced_date="2026-05-01"),
])
db.dedupe_cases()
check("年度またぎ2件が残る", len(db.list_cases(q="構内配電設備点検")) == 2)

print("[4] 申請つきの行は消えない")
db.upsert_cases([
    case("D1", title="外灯LED化工事", announced_date="2026-06-01"),
    case("D2", title="外灯LED化工事", announced_date="2026-06-05",
         detail_url="https://example.com/d2"),
])
d1 = db.get_case_id_by_external("D1")
db.set_application(d1, "参加申請準備前", note="申請中")
db.dedupe_cases()
check("申請つきD1が残っている", db.get_case_id_by_external("D1") == d1)
apps = [a for a in db.list_applications(None) if a.get("external_id") == "D1"]
check("申請レコードも無事", len(apps) == 1 and apps[0]["note"] == "申請中")

print("[5] 落札実績の winner が公告行へ引き継がれる")
db.upsert_cases([
    case("E1", title="変電設備更新工事", announced_date="2026-05-01",
         detail_url="https://example.com/e1", deadline="2026-06-01"),
    case("E2", title="変電設備更新工事", source="調達ポータル落札実績",
         announced_date="2026-10-01",  # 公告から153日後の落札発表（広い日付窓で突合）
         winner="株式会社テスト電工", win_price="12,000,000"),
])
db.dedupe_cases()
rows = db.list_cases(q="変電設備更新工事")
check("1件に統合される", len(rows) == 1)
check("公告行に落札者が付く",
      rows[0]["detail_url"] == "https://example.com/e1"
      and rows[0]["winner"] == "株式会社テスト電工")

print("[5b] 実データ監査で見つけた重複原因の統合（2026-07-27）")
db.upsert_cases([
    # 原因1+3: 閉札後の「【終了しました】」再収集（同一ページURL・公告日だけ更新）
    case("F1", title="一般競争入札の実施（合同庁舎ソーラー設置工事）",
         announced_date="2025-09-01", deadline="2025-09-05",
         detail_url="https://example.jp/f/232789.html"),
    case("F2", title="【終了しました】一般競争入札の実施（合同庁舎ソーラー設置工事）",
         announced_date="2025-09-25", detail_url="https://example.jp/f/232789.html"),
    case("F3", title="【終了しました】一般競争入札の実施（合同庁舎ソーラー設置工事）",
         announced_date="2026-07-01", detail_url="https://example.jp/f/232789.html"),
    # 原因2: 再公告（締切が変わる）
    case("G1", title="津波観測施設基礎等設置工事", announced_date="2026-07-01",
         deadline="2026-07-20", detail_url="https://example.jp/g1"),
    case("G2", title="津波観測施設基礎等設置工事（再度公告）", announced_date="2026-07-22",
         deadline="2026-08-08", detail_url="https://example.jp/g2"),
    # 原因4: 省庁/地方支分部局の二重登録（締切一致なら統合・具体名を残す）
    case("H1", title="法務局高圧設備改修他工事", agency="法務省",
         announced_date="2026-06-01", deadline="2026-07-01"),
    case("H2", title="法務局高圧設備改修他工事", agency="法務省札幌法務局",
         announced_date="2026-06-01", deadline="2026-07-01"),
    # 機関名が包含関係でも締切が違えば別案件（別組織の同名調達を守る）
    case("H3", title="保守点検業務", agency="大阪府",
         announced_date="2026-06-01", deadline="2026-06-20"),
    case("H4", title="保守点検業務", agency="大阪府警察本部",
         announced_date="2026-06-01", deadline="2026-07-20"),
    # 【地区名】は案件の識別子なので統合しない
    case("I1", title="【A地区】外構電気工事", announced_date="2026-06-01"),
    case("I2", title="【B地区】外構電気工事", announced_date="2026-06-01"),
])
db.dedupe_cases()
rows = db.list_cases(q="合同庁舎ソーラー設置工事")
check("【終了しました】3行→1行・元タイトルと締切が残る",
      len(rows) == 1 and rows[0]["title"].startswith("一般競争")
      and rows[0]["deadline"] == "2025-09-05")
rows = db.list_cases(q="津波観測施設基礎等設置工事")
check("再公告は締切違いでも統合され新しい公告が残る",
      len(rows) == 1 and "再度公告" in rows[0]["title"]
      and rows[0]["deadline"] == "2026-08-08")
rows = db.list_cases(q="法務局高圧設備改修他工事")
check("省庁/支分部局の二重登録は統合され具体的な機関名になる",
      len(rows) == 1 and rows[0]["agency"] == "法務省札幌法務局")
check("機関包含でも締切が違えば別案件のまま", len(db.list_cases(q="保守点検業務")) == 2)
check("【A地区】/【B地区】は別案件のまま", len(db.list_cases(q="外構電気工事")) == 2)

print("[6] AI使用量の加算（月×機能×モデル）＋押した回数")
db.add_ai_usage("応募アシスト", "gemini-2.5-flash", 1000, 200)
db.add_ai_usage("応募アシスト", "gemini-2.5-flash", 500, 100)
db.add_ai_usage("NG理由集計", "gemini-2.5-flash", 300, 50)
# 3タップ中、実際のAPI呼び出し（課金）は上の2回＝1回はキャッシュ応答の想定
db.add_ai_tap("応募アシスト")
db.add_ai_tap("応募アシスト")
db.add_ai_tap("応募アシスト")
usage = db.list_ai_usage()
assist_calls = [u for u in usage if u["kind"] == "応募アシスト" and u["model"]]
check("機能・モデルごとに行が分かれる", len(usage) == 3 and len(assist_calls) == 1)
check("回数・トークンが加算される",
      assist_calls[0]["calls"] == 2
      and assist_calls[0]["prompt_tokens"] == 1500
      and assist_calls[0]["output_tokens"] == 300)
check("押した回数はキャッシュ応答も含めて数える",
      sum(u["taps"] for u in usage if u["kind"] == "応募アシスト") == 3)

print("[7] Supabase 往復（スタブ）で集計が消えない")
snapshot = db.list_ai_usage()


class _FakeSupa:
    def enabled(self) -> bool:
        return True

    def load(self, key: str) -> Any:
        return snapshot if key == "ai_usage" else None

    def save(self, key: str, obj: Any) -> bool:
        return True


with db._connect() as conn:
    conn.execute("DELETE FROM ai_usage")
    conn.commit()
_real_supa = db.supa
db.supa = _FakeSupa()  # type: ignore[assignment]
try:
    counts = db.restore_from_supa()
finally:
    db.supa = _real_supa
check("復元で集計が戻る",
      counts.get("ai_usage") == len(snapshot)
      and db.list_ai_usage() == snapshot)

snapshot = []
db.supa = _FakeSupa()  # type: ignore[assignment]
try:
    db.restore_from_supa()
finally:
    db.supa = _real_supa
check("空復元でも集計が残る", len(db.list_ai_usage()) == 3)

print(f"\npassed {_ok} / failed {_ng}")
raise SystemExit(1 if _ng else 0)
