"""お客様要望 2026-08-20 の2件を守る回帰テスト。

  A. AI判定（〇△✕）→ 管理シートへの振り分け
     〇→参加申請準備前 / △→保留 / ✕→NG。
     **既に管理シートにある案件は絶対に書き換えない**（人の入力を守る不変条件）。
  B. 機関種別（地方公共団体 等）でまとめて案件一覧から外せる
     ＋ 監視機関の個別チェックが「案件に出てくる機関名」で実際に効くこと
     （従来は agencies テーブルの機関名しか出しておらず、外しても消えなかった）。

依存: Flask。DBは一時ファイルを使う。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# テストの書き込みが本番Supabaseへ飛ばないよう、db を import する前に無効化する。
os.environ.pop("SUPABASE_DB_URL", None)

import db  # noqa: E402

assert not db.supa.enabled(), "supa must be disabled during tests"

db.DB_PATH = Path(tempfile.mkdtemp()) / "test_verdict.db"
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


def make_case(ext: str, *, agency: str, agency_type: str) -> int:
    db.upsert_cases([{
        "source": "kkj", "external_id": ext, "title": f"{ext} 電気設備工事",
        "agency": agency, "agency_type": agency_type, "category": "電気工事-電気設備",
    }])
    cid = db.get_case_id_by_external(ext)
    assert cid is not None
    return cid


def set_verdict(ext: str, verdict: str) -> None:
    db.set_ai_assist(ext, json.dumps({"eligibility": {"verdict": verdict}}), "test")


# ============================================================
print("[A] AI判定 → 管理シートへの振り分け")
# ============================================================

ids = {}
for ext, verdict, agency_type in (
    ("v-maru", "〇", "地方公共団体"),
    ("v-sankaku", "△", "地方公共団体"),
    ("v-batsu", "✕", "国の機関"),
    ("v-hatena", "？", "国の機関"),
    ("v-nojudge", "", "国の機関"),
):
    ids[ext] = make_case(ext, agency=f"{ext}市", agency_type=agency_type)
    if verdict:
        set_verdict(ext, verdict)

r = client.post("/ai/route-verdicts", json={"case_ids": list(ids.values())})
body = r.get_json()
check("APIが200で応答する", r.status_code == 200)

status_of = {a["external_id"]: a["status"] for a in db.list_applications(None)}
check("〇 → 参加申請準備前", status_of.get("v-maru") == "参加申請準備前")
check("△ → 保留", status_of.get("v-sankaku") == "保留")
check("✕ → NG", status_of.get("v-batsu") == "NG")
check("？（判定不能）は振り分けない", "v-hatena" not in status_of)
check("未判定の案件は振り分けない", "v-nojudge" not in status_of)
check("振り分け件数が3件", body.get("added") == 3)
check("内訳が返る", body.get("counts") == {"〇": 1, "△": 1, "✕": 1})
check("追加済み external_id が返る（一覧の表示更新用）",
      set(body.get("added_eids") or []) == {"v-maru", "v-sankaku", "v-batsu"})
check("NG の external_id が返る", body.get("ng_eids") == ["v-batsu"])

# --- 不変条件: 既にシートにある案件は書き換えない ---
db.set_application(ids["v-maru"], "入札書提出済み", assignee="社長",
                   note="協力会社に見積依頼済み", bid_plan=1234000)
set_verdict("v-maru", "✕")           # 判定をやり直して✕になったとする
r2 = client.post("/ai/route-verdicts", json={"case_ids": list(ids.values())})
body2 = r2.get_json()
after = {a["external_id"]: a for a in db.list_applications(None)}
check("既存案件の状況を上書きしない", after["v-maru"]["status"] == "入札書提出済み")
check("既存案件の担当者を消さない", after["v-maru"]["assignee"] == "社長")
check("既存案件のメモを消さない", after["v-maru"]["note"] == "協力会社に見積依頼済み")
check("既存案件の入札予定額を消さない", after["v-maru"]["bid_plan"] == 1234000)
check("2回目は新規追加0件", body2.get("added") == 0)
check("既存分は skipped に数える", body2.get("skipped") == 3)

# --- 「保留」がカンバンの正式な列として使えること ---
check("保留が APP_STATUSES にある", "保留" in db.APP_STATUSES)
check("保留は参加申請準備前より前の列", db.APP_STATUSES.index("保留") <
      db.APP_STATUSES.index("参加申請準備前"))
check("保留に色が割り当たっている", bool(db.STATUS_ACCENT.get("保留")))
try:
    db.set_application(ids["v-sankaku"], "保留", note="仕様書待ち")
    check("保留は手で選んでも保存できる",
          db.get_application(ids["v-sankaku"])["status"] == "保留")
except ValueError:
    check("保留は手で選んでも保存できる", False)

# --- 存在しない case_id を投げても落ちない ---
r3 = client.post("/ai/route-verdicts", json={"case_ids": [999999, "abc", None]})
check("不正な case_id でも落ちない", r3.status_code == 200 and r3.get_json()["added"] == 0)

# ============================================================
print("[B] 機関種別でまとめて外す")
# ============================================================

n_local = db.count_list_cases(exclude_org_types=["地方公共団体"])
n_all = db.count_list_cases()
check("除外なしでは全件出る", n_all == 5)
check("地方公共団体を外すと2件減る", n_local == 3)
check("外した種別の案件は一覧に出ない",
      all(c["agency_type"] != "地方公共団体"
          for c in db.list_cases(exclude_org_types=["地方公共団体"])))

# 機関種別が空の案件は「種別が判らないだけ」なので消さない（取りこぼし防止）
make_case("v-unknown", agency="種別不明機構", agency_type="")
check("機関種別が空の案件は除外されない",
      any(c["external_id"] == "v-unknown"
          for c in db.list_cases(exclude_org_types=["地方公共団体"])))

# 保存と復元
db.set_org_type_excluded("地方公共団体", excluded=True)
check("除外がDBに残る", db.list_org_type_exclusions() == {"地方公共団体"})
r4 = client.post("/agencies/org-types/toggle",
                 json={"name": "国の機関", "included": False})
check("APIで除外を追加できる",
      r4.status_code == 200 and db.list_org_type_exclusions() == {"地方公共団体", "国の機関"})
r5 = client.post("/agencies/org-types/toggle",
                 json={"name": "国の機関", "included": True})
check("APIで除外を解除できる",
      r5.status_code == 200 and db.list_org_type_exclusions() == {"地方公共団体"})
client.post("/agencies/org-types/restore", json={"excluded": ["市区町村"]})
check("localStorage からの復元で置き換わる", db.list_org_type_exclusions() == {"市区町村"})
client.post("/agencies/org-types/restore", json={"excluded": []})

# 一覧画面に実際に効いているか（HTMLの該当件数まで見る）
db.set_org_type_excluded("地方公共団体", excluded=True)
# クエリ無しだと初期表示が「近畿」固定になるので、明示的に全国で開く
html = client.get("/?region=").get_data(as_text=True)
check("案件一覧に除外した機関の案件が出ない", "v-maru市" not in html)
check("除外していない機関の案件は出る", "v-batsu市" in html)
db.set_org_type_excluded("地方公共団体", excluded=False)

# ============================================================
print("[C] 監視機関の個別チェックが実際に効くこと")
# ============================================================

case_agencies = {a["name"] for a in db.list_case_agencies()}
check("監視機関一覧が案件側の機関名を出す", "v-maru市" in case_agencies)
check("機関種別も一緒に出る",
      any(a["name"] == "v-maru市" and a["agency_type"] == "地方公共団体"
          for a in db.list_case_agencies()))
db.set_agency_excluded("v-maru市", excluded=True)
check("外した機関の案件が一覧から消える",
      all(c["agency"] != "v-maru市"
          for c in db.list_cases(exclude_agencies=db.list_agency_exclusions())))
page = client.get("/agencies").get_data(as_text=True)
check("監視機関ページが開ける（案件由来）", "v-maru市" in page)
check("機関種別のチェックが画面に出る", "地方公共団体" in page)
page_all = client.get("/agencies?scope=all").get_data(as_text=True)
check("NJSS全国リストにも切り替えられる", page_all.count("NJSS全国リスト") >= 1)
db.set_agency_excluded("v-maru市", excluded=False)

print(f"\n{_ok}/{_ok + _ng} passed")
raise SystemExit(1 if _ng else 0)
