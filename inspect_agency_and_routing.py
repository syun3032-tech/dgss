"""お客様要望 2026-08-20 の2件を、本物のDBに対して検品する。

テストは「壊れていないこと」しか見ない。ここでは**実データで意図どおりの中身に
なっているか**を数字で出す。使い方:

    .venv/bin/python inspect_uenishi_requests.py [DBパス]

指摘が1件でもあれば終了コード1。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.pop("SUPABASE_DB_URL", None)   # 検品で本番KVに触らない

import db  # noqa: E402

if len(sys.argv) > 1:
    db.DB_PATH = Path(sys.argv[1])

_issues: list[str] = []
_checks = 0


def expect(name: str, cond: bool, detail: str = "") -> None:
    global _checks
    _checks += 1
    if cond:
        print(f"  ok   {name}" + (f"  … {detail}" if detail else ""))
    else:
        print(f"  NG   {name}" + (f"  … {detail}" if detail else ""))
        _issues.append(name)


print(f"DB: {db.DB_PATH}")
total = db.count_cases()
print(f"案件 {total:,} 件\n")

# ------------------------------------------------------------
print("[1] 機関種別（地方公共団体をまとめて外せるか）")
# ------------------------------------------------------------
org_types = db.list_org_types()
for t in org_types:
    print(f"       {t['name']:<12} {t['count']:>7,} 件  ({t['count'] * 100 // max(total, 1)}%)")
names = {t["name"] for t in org_types}
expect("機関種別が1つ以上ある（外す選択肢が画面に出る）", bool(org_types))
expect("『地方公共団体』が選択肢にある（お客様が外したい種別）",
       "地方公共団体" in names)

typed = sum(t["count"] for t in org_types)
untyped = total - typed
expect("機関種別が空の案件が全体の3割未満",
       untyped < total * 0.3, f"種別不明 {untyped:,} 件")

if "地方公共団体" in names:
    base = db.count_list_cases()
    after = db.count_list_cases(exclude_org_types=["地方公共団体"])
    n_local = next(t["count"] for t in org_types if t["name"] == "地方公共団体")
    expect("地方公共団体を外すと、ちょうどその件数だけ減る",
           base - after == n_local, f"{base:,} → {after:,}（-{base - after:,}）")
    expect("外しても案件が全部消えるわけではない",
           after > 0, f"残り {after:,} 件")
    left = db.list_cases(exclude_org_types=["地方公共団体"], limit=500)
    expect("残った案件に地方公共団体が1件も混ざらない",
           all(c["agency_type"] != "地方公共団体" for c in left))
    # 種別が空の案件は「判らないだけ」なので消してはいけない
    if untyped:
        kept = db.count_list_cases(exclude_org_types=list(names))
        expect("全種別を外しても、種別不明の案件は残る（取りこぼし防止）",
               kept == untyped, f"{kept:,} 件")

# ------------------------------------------------------------
print("\n[2] 監視機関のチェックが実際に効くか")
# ------------------------------------------------------------
case_agencies = db.list_case_agencies(limit=100000)
njss_names = {a["name"] for a in db.list_agencies()}
case_names = {a["name"] for a in case_agencies}
hit = len(case_names & njss_names)
print(f"       案件に出てくる発注機関 {len(case_names):,} 種")
print(f"       NJSS全国リスト        {len(njss_names):,} 機関")
print(f"       名前が一致するのは    {hit:,} 機関"
      f"（{hit * 100 // max(len(case_names), 1)}%）← 旧・監視機関ページはこちらだけを出していた")

expect("監視機関ページが案件由来の機関名を出す（外せば必ず効く）",
       len(case_agencies) > 0, f"{len(case_agencies):,} 機関")
expect("案件由来の機関名は100%が案件に紐づく",
       all(a["count"] > 0 for a in case_agencies))
expect("機関種別が付いている機関が過半数",
       sum(1 for a in case_agencies if a["agency_type"]) > len(case_agencies) * 0.5,
       f"{sum(1 for a in case_agencies if a['agency_type']):,}/{len(case_agencies):,}")

if case_agencies:
    top = case_agencies[0]
    kept = db.count_list_cases(exclude_agencies=[top["name"]])
    expect(f"1機関（{top['name']}）を外すと、その件数だけ減る",
           db.count_list_cases() - kept == top["count"],
           f"-{db.count_list_cases() - kept:,} 件")

# 除外中なのに一覧に出てこない機関（設定が見えないまま効き続ける事故の芽）
orphans = db.list_agency_exclusions() - case_names
if orphans:
    print(f"       ※ 除外中だが今の案件に出てこない機関 {len(orphans)} 件"
          f"（監視機関ページに「今の案件データには出てきません」と表示される）")

# ------------------------------------------------------------
print("\n[3] AI判定 → 管理シートの振り分け")
# ------------------------------------------------------------
expect("『保留』がカンバンの列にある（△の受け皿）", "保留" in db.APP_STATUSES)
expect("『保留』は参加申請準備前より前の列",
       "保留" in db.APP_STATUSES and "参加申請準備前" in db.APP_STATUSES
       and db.APP_STATUSES.index("保留") < db.APP_STATUSES.index("参加申請準備前"))
expect("〇△✕の3つとも行き先が決まっている",
       set(db.VERDICT_STATUS) == {"〇", "△", "✕"}, str(db.VERDICT_STATUS))
expect("行き先の状況はすべて実在の列",
       all(s in db.APP_STATUSES_ALL for s in db.VERDICT_STATUS.values()))

apps = db.list_applications(None)
by_status: dict[str, int] = {}
for a in apps:
    by_status[a["status"]] = by_status.get(a["status"], 0) + 1
print(f"       管理シート {len(apps):,} 件")
for s in db.APP_STATUSES_ALL:
    if by_status.get(s):
        print(f"       　{s:<16} {by_status[s]:>5,} 件")
unknown = [s for s in by_status if s not in db.APP_STATUSES_ALL]
expect("管理シートに未知の状況が無い", not unknown, str(unknown))

print(f"\n検品 {_checks} 項目 / 指摘 {len(_issues)} 件")
for i in _issues:
    print(f"  - {i}")
raise SystemExit(1 if _issues else 0)
