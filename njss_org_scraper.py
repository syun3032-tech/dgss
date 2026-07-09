"""NJSS 発注機関一覧（www2.njss.info/organizations/search）スクレイパー。

NJSSの「発注機関を探す」は公開SEOページだが AWS WAF のJSチャレンジ付きのため、
curl不可・Playwright（実ブラウザ）必須。1ページ100件で総数分ページングする。

【重要】未ログイン閲覧は1日あたり数ページで「閲覧できる上限回数に達しました」に
なる。上限に達したら素直に中断し、翌日以降に続きから再開する（回避はしない）。
そのため出力CSVは追記マージ式（proc_id で重複排除）で、何日かに分けて完走する。

用途: クライアント要望「NJSS掲載の発注機関を監視機関へ順次追加」
（まず独立行政法人 category=114 の851件、将来的に総数9,041件）。

organization_category の例（一覧ページのパンくずから判明した分）:
  114 = 独立行政法人（認可法人 ⊂ 外郭団体等 ⊂ 国・省庁）

CLI:
  .venv/bin/python njss_org_scraper.py --category 114 --out research/njss_orgs_114.csv
"""

from __future__ import annotations

import csv
import os
import re

BASE = "https://www2.njss.info/organizations/search"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
LIMIT = 100  # 1ページ最大件数（limit=100 が上限）

# 一覧の各行からブラウザ内で抽出するJS。行= .SearchResultList__ListItem
_EXTRACT_JS = """
() => [...document.querySelectorAll('.SearchResultList .ListItem__Container')].map(item => {
  const a = item.querySelector("h2 a[href*='/organizations/proc/']");
  const addr = item.querySelector('.SearchItem__Address');
  return {
    name: a ? a.textContent.trim() : '',
    href: a ? a.getAttribute('href') : '',
    address: addr ? addr.textContent.trim() : '',
    text: item.innerText,
  };
}).filter(r => r.name)
"""


def _num(text: str) -> int:
    return int(text.replace(",", "")) if text else 0


def _parse_counts(text: str) -> dict:
    """行テキストから 受付中/登録案件数/入札結果数 を抜く。"""
    m_open = re.search(r"受付中\s*([\d,]+)\s*件", text)
    m_total = re.search(r"登録案件数\s*([\d,]+)\s*件", text.replace("\n", ""))
    m_result = re.search(r"入札結果数\s*([\d,]+)\s*件", text.replace("\n", ""))
    return {
        "open_count": _num(m_open.group(1)) if m_open else 0,
        "total_count": _num(m_total.group(1)) if m_total else 0,
        "result_count": _num(m_result.group(1)) if m_result else 0,
    }


CSV_COLS = ["name", "proc_id", "njss_url", "address",
            "open_count", "total_count", "result_count"]


def _is_limited(body_text: str) -> bool:
    """未ログイン閲覧の上限ページかどうか。"""
    return "閲覧できる上限回数に達しました" in body_text


def load_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def save_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        w.writerows({c: r.get(c, "") for c in CSV_COLS} for r in rows)


def fetch_organizations(category: str = "114", out: str = "research/njss_orgs_114.csv",
                        max_pages: int | None = None, headless: bool = True,
                        timeout_ms: int = 60000) -> tuple[list[dict], bool]:
    """指定カテゴリの発注機関を取得し out にマージ保存する（レジューム式）。

    既取得の proc_id はスキップ。閲覧上限に達したらそこで中断して
    (現在の全行, 完走したか) を返す。完走まで日をまたいで再実行する。
    """
    from playwright.sync_api import sync_playwright

    rows = load_csv(out)
    seen = {r["proc_id"] for r in rows}
    # 並びは -opening_count 固定。日をまたぐと順位が動き得るが proc_id 重複排除で
    # 二重登録はしない（取りこぼしは完走後にもう1周して埋める）。
    start_page = (len(rows) // LIMIT) + 1
    completed = False
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        ctx = browser.new_context(user_agent=UA, locale="ja-JP")
        page = ctx.new_page()
        page_no, total, fetched_pages = start_page, None, 0
        while True:
            url = f"{BASE}?organization_category={category}&sort=-opening_count&page={page_no}&limit={LIMIT}"
            page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            # Vue描画待ち: 行が出るか上限表示が出るまで最大15秒
            for _ in range(10):
                body = page.inner_text("body")
                items = page.evaluate(_EXTRACT_JS)
                if items or _is_limited(body):
                    break
                page.wait_for_timeout(1500)
            if _is_limited(body):
                print(f"  page {page_no}: 閲覧上限に達したため中断（累計 {len(rows)} 件）。"
                      "翌日以降に再実行してください")
                break
            if total is None:
                m = re.search(r"([\d,]+)\s*件", body)
                total = _num(m.group(1)) if m else 0
                print(f"カテゴリ{category}: 総数 {total} 件 / 既取得 {len(rows)} 件 / page {page_no} から")
            new = 0
            for it in items:
                m = re.search(r"/organizations/proc/(\d+)", it["href"] or "")
                if not m or m.group(1) in seen:
                    continue
                seen.add(m.group(1))
                new += 1
                rows.append({
                    "name": it["name"],
                    "proc_id": m.group(1),
                    "njss_url": f"https://www2.njss.info/organizations/proc/{m.group(1)}",
                    "address": it["address"],
                    **_parse_counts(it["text"]),
                })
            print(f"  page {page_no}: +{new} 件（累計 {len(rows)}）")
            save_csv(rows, out)  # 1ページごとに保存（中断しても消えない）
            page_no += 1
            fetched_pages += 1
            if len(rows) >= (total or 0) or new == 0:
                completed = True
                break
            if max_pages and fetched_pages >= max_pages:
                break
            page.wait_for_timeout(1500)  # 行儀よく間隔を空ける
        browser.close()
    save_csv(rows, out)
    return rows, completed


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="NJSS 発注機関一覧の取得（レジューム式）")
    ap.add_argument("--category", default="114", help="organization_category（114=独立行政法人）")
    ap.add_argument("--out", default="research/njss_orgs_114.csv")
    ap.add_argument("--max-pages", type=int, default=None)
    args = ap.parse_args()

    orgs, done = fetch_organizations(category=args.category, out=args.out,
                                     max_pages=args.max_pages)
    state = "完走" if done else "途中（閲覧上限）"
    print(f"{state}: {len(orgs)} 機関を {args.out} に保存済み")
