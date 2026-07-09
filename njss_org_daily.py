"""NJSS発注機関リストの日次自動取得（ローカルlaunchd用・レジューム式）。

NJSSの未ログイン閲覧は1日数ページで上限になるため、毎日このスクリプトが
「上限に達するまで続きを取得 → URL解決 → CSV再生成 → git commit（pushはしない）」
を繰り返し、数日かけて完走させる（独法851件→将来は他カテゴリも）。

- 取得済みなら何もしない（完走後は実質no-op、リンク検証だけ行う）
- commit はローカルのみ。push は人が行う（勝手にリモートへ出さない）
"""

from __future__ import annotations

import csv
import subprocess

import njss_org_resolver as resolver
import njss_org_scraper as scraper

CATEGORY = ""  # 空=全カテゴリ（発注機関 全9,041件）。当初は "114"=独法のみだった
ORGS_CSV = "research/njss_orgs_all.csv"
AGENCIES_CSV = "research/njss_dokuho_agencies.csv"
REPORT_CSV = "research/njss_dokuho_report.csv"


def main() -> None:
    before = len(scraper.load_csv(ORGS_CSV))
    try:
        rows, done = scraper.fetch_organizations(category=CATEGORY, out=ORGS_CSV)
    except Exception as e:  # noqa: BLE001 — 取得失敗日はスキップ（翌日再試行）
        print(f"[NJSS機関] 取得失敗（スキップ）: {str(e)[:80]}")
        rows, done = scraper.load_csv(ORGS_CSV), False
    print(f"[NJSS機関] {before} → {len(rows)} 件 {'(完走)' if done else '(続きは翌日)'}")

    if not rows:
        return
    orgs = [dict(r) for r in rows]
    for o in orgs:
        for k in ("open_count", "total_count", "result_count"):
            o[k] = int(o.get(k) or 0)
    existing = resolver.load_existing_agencies()
    orgs = resolver.resolve(orgs, existing)
    n_ok, n_all = resolver.write_outputs(orgs, AGENCIES_CSV, REPORT_CSV)
    print(f"[NJSS機関] 解決: 追加可 {n_ok} / 全 {n_all}")

    if len(rows) > before:  # 増えた日だけローカルcommit（pushは人が判断）
        subprocess.run(["git", "add", ORGS_CSV, AGENCIES_CSV, REPORT_CSV], check=False)
        subprocess.run(
            ["git", "commit", "-m",
             f"data: NJSS発注機関リスト更新（{len(rows)}件・自動取得）"],
            check=False,
        )


if __name__ == "__main__":
    main()
