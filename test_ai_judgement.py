"""お客様要望 2026-09-07（AI判定が△だらけ／理由が残らない）を守る回帰テスト。

  A. 「対応エリア（施工に行ける範囲）」と「本店・支店の所在地（地域要件）」を別に持つ
  B. 判定ルールが「書かれていない要件で△・✕にしない」ことを明文化している
  C. △（保留）に振り分けたとき、理由と不足情報が管理シートのメモに残る
  D. 再判定は「AIが置いただけの行」の状況を更新し、人が触った行は動かさない
  E. AI判定への追加指示をその場で保存でき、プロンプトへ渡る

依存: Flask。DBは一時ファイルを使う。AIは呼ばない（プロンプト組み立てだけ検査）。
"""
from __future__ import annotations

import io

import json
import os
import tempfile
from pathlib import Path

# テストの書き込みが本番Supabaseへ飛ばないよう、db を import する前に無効化する。
os.environ.pop("SUPABASE_DB_URL", None)

import db  # noqa: E402

assert not db.supa.enabled(), "supa must be disabled during tests"

db.DB_PATH = Path(tempfile.mkdtemp()) / "test_ai_judge.db"
db.init_db()

import ai_assist  # noqa: E402
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


def make_case(ext: str) -> int:
    db.upsert_cases([{
        "source": "kkj", "external_id": ext, "title": f"{ext} 電気設備工事",
        "agency": "大阪府", "agency_type": "地方公共団体", "prefecture": "大阪府",
        "category": "電気工事-電気設備",
    }])
    cid = db.get_case_id_by_external(ext)
    assert cid is not None
    return cid


def set_judge(ext: str, payload: dict) -> None:
    db.set_ai_assist(ext, json.dumps(payload, ensure_ascii=False), "test")


# ============================================================
print("[A] 対応エリアと 本支店の所在地 を分けて持てる")
# ============================================================

d = db.default_profile()
check("既定の本支店所在地は大阪府", d["office_prefectures"] == "大阪府")
check("既定の対応エリアは全国（空＝限定なし）", d["prefectures"] == "")

db.save_profile(prefectures="", categories="電気工事", budget_max="", grade="B",
                quals="建設業許可（電気工事業）", company="テスト電気（株）",
                office_prefectures="大阪府", ai_instructions="地域要件は大阪府のみで判定")
p = db.get_profile()
check("対応エリア=全国（空）で保存できる", p["prefectures"] == "")
check("本支店の所在地が別列に保存される", p["office_prefectures"] == "大阪府")
check("AIへの追加指示が保存される", p["ai_instructions"] == "地域要件は大阪府のみで判定")

make_case("aj-1")
check("対応エリアが空でもマッチ案件は0件にならない（全国扱い）",
      len(db.match_cases(p)) >= 1)
check("マッチ理由に『対応エリア（全国）』が出る",
      "対応エリア（全国）" in db.match_cases(p)[0]["match_reasons"])

# 本支店を空で保存しても、判定材料が空にならないよう既定で補う
db.save_profile(prefectures="", categories="電気工事", budget_max="", company="テスト電気（株）",
                office_prefectures="", ai_instructions="")
check("本支店が未設定なら既定（大阪府）を補う",
      db.get_profile()["office_prefectures"] == "大阪府")
db.save_profile(prefectures="", categories="電気工事", budget_max="", company="テスト電気（株）",
                office_prefectures="大阪府", ai_instructions="")

# ============================================================
print("[B] 判定ルール（書かれていない要件で△・✕にしない）")
# ============================================================

lines = ai_assist._profile_lines(db.get_profile())
check("プロンプトに『対応エリアは地域要件に使わない』と書いてある",
      "地域要件の判定に使わない" in lines)
check("プロンプトに本支店の所在地が渡る", "本店・支店・営業所の所在地: 大阪府" in lines)

sysmsg = ai_assist._SYSTEM
check("書かれていない要件を理由に△・✕にしない、と指示している",
      "書かれていない事項を理由に、△や✕にしてはならない" in sysmsg)
check("地域要件が無い案件は所在地を理由に減点しない、と指示している",
      "地域要件が書かれていない → 所在地・エリアを理由に減点しない" in sysmsg)
check("等級の記載が無い＝等級を理由に△にしない、と指示している",
      "等級を理由に△にしてはならない" in sysmsg)
check("△なら不足情報を必ず書かせている", "missing が空の△を出してはならない" in sysmsg)

el_schema = ai_assist._SCHEMA["properties"]["eligibility"]
req = el_schema["required"]
check("判定の主因(reason_code)が必須", "reason_code" in req)
check("公告の地域要件(region_requirement)が必須", "region_requirement" in req)
check("不足情報(missing)が必須", "missing" in req)

txt = ai_assist._build_user_text(
    {"title": "テスト工事"}, db.get_profile(), None,
    notice_text="公告本文テスト", spec_text="入札参加説明書の中身テスト",
    instructions="地域要件が無いなら〇にすること")
check("入札参加説明書のテキストがプロンプトに入る", "入札参加説明書の中身テスト" in txt)
check("追加指示がプロンプトに入る", "地域要件が無いなら〇にすること" in txt)
check("追加指示は最優先と明記される", "最優先" in txt)

# ============================================================
print("[C] △の理由が管理シートに残る")
# ============================================================

cid_maru = make_case("aj-maru")
cid_san = make_case("aj-san")
cid_batsu = make_case("aj-batsu")
set_judge("aj-maru", {"eligibility": {
    "verdict": "〇", "reason_code": "条件を満たす", "region_requirement": "地域要件なし",
    "reasons": ["等級: 公告に記載なし", "地域要件の記載なし＝要件なしとして扱う"], "missing": []}})
set_judge("aj-san", {"eligibility": {
    "verdict": "△", "reason_code": "情報不足・その他", "region_requirement": "地域要件なし",
    "reasons": ["参加資格が『別紙のとおり』とだけ記載"],
    "missing": ["参加資格の等級要件（入札参加説明書 3.参加資格 を取得して確認）"]}})
set_judge("aj-batsu", {"eligibility": {
    "verdict": "✕", "reason_code": "地域要件",
    "region_requirement": "本店または支店が北海道内にあること",
    "reasons": ["地域要件: 要求 北海道・自社拠点 大阪府"], "missing": []}})

r = client.post("/ai/route-verdicts", json={"case_ids": [cid_maru, cid_san, cid_batsu]})
check("振り分けAPIが200", r.status_code == 200)
notes = {a["external_id"]: a["note"] for a in db.list_applications(None)}
stat = {a["external_id"]: a["status"] for a in db.list_applications(None)}
check("△は保留に入る", stat.get("aj-san") == "保留")
check("△のメモに判定と主因が残る", notes.get("aj-san", "").startswith("【AI判定: △／情報不足・その他】"))
check("△のメモに『何が足りないか』が残る", "入札参加説明書" in notes.get("aj-san", ""))
check("△のメモに不足情報の見出しが付く", "［不足情報］" in notes.get("aj-san", ""))
check("✕のメモに地域要件がそのまま残る",
      "本店または支店が北海道内にあること" in notes.get("aj-batsu", ""))
check("〇のメモにも根拠が残る", "【AI判定: 〇" in notes.get("aj-maru", ""))

v = client.get(f"/ai/verdicts?ids={cid_san},{cid_batsu}").get_json()
check("一覧APIが判定と理由を返す", v[str(cid_san)]["verdict"] == "△")
check("一覧APIが主因コードを返す", v[str(cid_batsu)]["code"] == "地域要件")
check("△は『要確認: 不足情報』を返す", v[str(cid_san)]["why"].startswith("要確認: "))

# ============================================================
print("[D] 再判定＝AIが置いた行だけ更新。人が触った行は動かさない")
# ============================================================

# 保留に入っていた △ が、説明書を読んで ✕ に変わったとする
set_judge("aj-san", {"eligibility": {
    "verdict": "✕", "reason_code": "等級・ランク不足",
    "region_requirement": "地域要件なし",
    "reasons": ["等級不足: 要求A・自社C"], "missing": []}})
r2 = client.post("/ai/route-verdicts",
                 json={"case_ids": [cid_san], "update_ai_routed": True})
after = {a["external_id"]: a for a in db.list_applications(None)}
check("再判定でAIが置いた行の状況が更新される", after["aj-san"]["status"] == "NG")
check("再判定でメモも新しい理由に入れ替わる", "等級不足: 要求A・自社C" in after["aj-san"]["note"])
check("更新件数が返る", r2.get_json().get("updated") == 1)

# 人が触った行は動かさない
db.set_application(cid_maru, "入札書提出済み", assignee="社長", note="協力会社に見積依頼済み",
                   bid_plan=1234000)
set_judge("aj-maru", {"eligibility": {
    "verdict": "✕", "reason_code": "地域要件", "region_requirement": "大阪市内に本店",
    "reasons": ["地域要件: 要求 大阪市・自社拠点 八尾市"], "missing": []}})
r3 = client.post("/ai/route-verdicts",
                 json={"case_ids": [cid_maru], "update_ai_routed": True})
after = {a["external_id"]: a for a in db.list_applications(None)}
check("人が進めた状況は再判定でも変えない", after["aj-maru"]["status"] == "入札書提出済み")
check("人が書いたメモは再判定でも消さない",
      after["aj-maru"]["note"] == "協力会社に見積依頼済み")
check("人が入れた担当者を消さない", after["aj-maru"]["assignee"] == "社長")
check("人が入れた入札予定額を消さない", after["aj-maru"]["bid_plan"] == 1234000)
check("動かせなかった分は updated に数えない", r3.get_json().get("updated") == 0)

check("AI管理下の判定関数: 人の入力があれば False",
      db.is_ai_owned_application(after["aj-maru"]) is False)
check("AI管理下の判定関数: AIのメモだけなら True",
      db.is_ai_owned_application(after["aj-san"]) is True)

# ============================================================
print("[E] AI判定への追加指示をその場で保存できる")
# ============================================================

before = db.get_profile()
r4 = client.post("/ai/instructions",
                 json={"instructions": "対応エリアは全国。地域要件が無い案件は〇にする"})
check("保存APIが200", r4.status_code == 200 and r4.get_json().get("ok") is True)
now = db.get_profile()
check("指示が保存される", now["ai_instructions"] == "対応エリアは全国。地域要件が無い案件は〇にする")
check("他のマイ条件を巻き添えで消さない",
      now["company"] == before["company"] and now["office_prefectures"] == before["office_prefectures"])
check("等級（機関別）も消えない", len(now["qualifications"]) == len(before["qualifications"]))
check("GETで現在値が読める",
      client.get("/ai/instructions").get_json()["instructions"] == now["ai_instructions"])

# ============================================================
print("[F] 対応エリアを全国（空）にしても設定が巻き戻らない")
# ============================================================
# ブラウザ保存(localStorage)からの自動復元は、新しい項目を知らない古いミラーを
# POSTしてくる。読み込めていないものを書き戻さない、が不変条件。

db.save_profile(prefectures="", categories="電気工事", budget_max="", company="テスト電気（株）",
                office_prefectures="大阪府", ai_instructions="地域要件は大阪府のみ",
                qualifications=db.default_qualifications())
with appmod.app.test_request_context("/"):
    check("対応エリアが空でも『マイ条件あり』と判定する（復元で巻き戻さない）",
          appmod.inject_profile_set()["profile_set"] is True)

# 古いミラー相当の部分POST（新項目なし・対応エリアは近畿）
client.post("/profile", data={
    "company": "テスト電気（株）", "prefectures": ["大阪府", "兵庫県"],
    "categories": ["電気工事"], "qualifications": "[]"})
p2 = db.get_profile()
check("部分POSTで本支店の所在地が消えない", p2["office_prefectures"] == "大阪府")
check("部分POSTでAIへの指示が消えない", p2["ai_instructions"] == "地域要件は大阪府のみ")

# 対応エリアを送ってこない部分POSTでは、現在の対応エリアを維持する
db.save_profile(prefectures="", categories="電気工事", budget_max="", company="テスト電気（株）",
                office_prefectures="大阪府", ai_instructions="地域要件は大阪府のみ")
client.post("/profile", data={"company": "テスト電気（株）", "categories": ["電気工事"],
                              "qualifications": "[]"})
check("部分POSTは対応エリアを勝手に書き換えない", db.get_profile()["prefectures"] == "")

# フォーム全体の送信なら、意図どおり全国（空）にもできるし本支店も空にできる
client.post("/profile", data={"full_form": "1", "company": "テスト電気（株）",
                              "categories": ["電気工事"], "qualifications": "[]",
                              "ai_instructions": "全国で判定"})
p3 = db.get_profile()
check("フォーム送信なら対応エリアを全国（空）にできる", p3["prefectures"] == "")
check("フォーム送信ならAIへの指示を書き換えられる", p3["ai_instructions"] == "全国で判定")
check("マイ条件フォームに full_form の印がある",
      'name="full_form"' in client.get("/profile").get_data(as_text=True))
check("ブラウザ保存の復元が新項目も送るようになっている",
      "b.append('office_prefectures', v)" in
      io.open("templates/base.html", encoding="utf-8").read())

# ============================================================
print("[G] 判定結果が消えない／再判定の対象が拾える")
# ============================================================

# 判定→軽量形で取り出せる（Supabaseに保存する形）
v = db.list_ai_verdicts()
check("判定を保存用の軽量形で取り出せる", "aj-san" in v and v["aj-san"]["verdict"] in "〇△✕")
check("軽量形に理由コードが入る", bool(v["aj-batsu"]["reason_code"]))
check("軽量形に不足情報が入る（△の理由）",
      isinstance(v.get("aj-maru", {}).get("missing"), list))
check("案件AI概要のキャッシュ(sum:)は判定に混ぜない",
      all(not k.startswith("sum:") for k in v))

# デプロイでDBが作り直された状況＝ai_assist が空 → 保存から戻す
with db._connect() as _c:
    _c.execute("DELETE FROM ai_assist")
    _c.commit()
check("消えた状態では判定が引けない", db.get_ai_assist("aj-batsu") is None)
n = db.restore_ai_verdicts(v)
check("保存から判定を戻せる", n >= 3)
back = json.loads(db.get_ai_assist("aj-batsu")["payload"])
check("戻した判定の〇△✕が一致する", (back["eligibility"] or {}).get("verdict") == "✕")
check("戻した判定の理由も残る", "地域要件" in (back["eligibility"] or {}).get("reason_code", ""))
check("戻した判定には『復元』の印が付く", back.get("restored") is True)

# その場で出し直した新しい判定を、古い保存で潰さない
set_judge("aj-batsu", {"eligibility": {"verdict": "〇", "reason_code": "条件を満たす",
                                       "region_requirement": "地域要件なし",
                                       "reasons": ["新しい判定"], "missing": []}})
db.restore_ai_verdicts(v)
now = json.loads(db.get_ai_assist("aj-batsu")["payload"])
check("既にある新しい判定は復元で上書きしない",
      (now["eligibility"] or {}).get("verdict") == "〇")

# 再判定の対象＝保留 ＋ AIが置いただけのNG。人が理由を書いたNGは対象外。
cid_hold = make_case("aj-hold")
cid_ai_ng = make_case("aj-aing")
cid_human_ng = make_case("aj-humanng")
db.set_application(cid_hold, "保留", note="【AI判定: △／情報不足・その他】…")
db.set_application(cid_ai_ng, "NG", note="【AI判定: ✕／地域要件】…")
db.set_application(cid_human_ng, "NG", note="＃230落札のため技術者が申請出来ない為")
tg = {t["case_id"] for t in db.list_ai_rejudge_targets()}
check("保留は再判定の対象", cid_hold in tg)
check("AIが付けただけのNGも再判定の対象", cid_ai_ng in tg)
check("人が理由を書いたNGは対象にしない", cid_human_ng not in tg)
r5 = client.get("/ai/rejudge-targets").get_json()
check("再判定の対象APIが件数を返す", r5["count"] == len(tg) and r5["hold"] >= 1 and r5["ng"] >= 1)
check("対象APIが case_id を返す", cid_ai_ng in r5["case_ids"])

# 締切が過ぎた案件は再判定しない（課金する意味が無い）
db.upsert_cases([{"source": "kkj", "external_id": "aj-old", "title": "期限切れ",
                  "agency": "大阪府", "deadline": "2020-01-01"}])
cid_old = db.get_case_id_by_external("aj-old")
db.set_application(cid_old, "保留", note="【AI判定: △】")
check("締切が過ぎた案件は再判定の対象にしない",
      cid_old not in {t["case_id"] for t in db.list_ai_rejudge_targets()})

print(f"\n{_ok}/{_ok + _ng} passed")
raise SystemExit(1 if _ng else 0)
