"""応募導線（どこで申し込むか）を「AIなし・完全コード」で決定するモジュール。

設計方針（重要）:
  クライアント提供スプレッドシートの "NJSS公示リンク先"（= agencies.bid_url）は
  「過去の公告の一例」であり、申し込み先ページではない。和歌山県庁が競輪サイト
  (keirinwa.com) になっている等、データ自体に誤りも混ざる。よって bid_url は
  応募導線に使わない。

  代わりに、次の優先順で「確実に正しい導線」だけを出す。
    1) 各案件が持つ実リンク（官公需APIの detail_url / spec_url, 全体の約92%）
       = その案件そのものの公告。応募資格・提出書類・締切・問い合わせが載る本命。
    2) 発注機関ドメインから判定できた「大手・安定の電子入札システム」だけ、
       手当てした正規ポータルURLを提示（KNOWN_PORTALS）。
       判定できないドメイン（誤データ含む）は何も出さず 3) に委ねる。
    3) 案件名・機関名・都道府県での "検索リンク"（常に最新・保守不要）。

  すべて純粋関数。実行時にAI・有料APIを一切使わない。データの鮮度は既存の
  日次更新（update.py --fast）が担う。本モジュールの定数は安定した公的ポータルのみ。
"""

from __future__ import annotations

import re
import urllib.parse

# 個別案件ではなく「検索トップ」等に着地してしまう汎用URL。
# 官公需APIの ExternalDocumentURI が空のとき等に返るランディングページ群。
GENERIC_URL_MARKERS = (
    "p-portal.go.jp/pps-web-biz/UAA01/OAA0101",   # 調達ポータル 検索トップ
    "i-ppi.jp/IPPI/SearchServices/Web/Index.htm",  # PPI 検索トップ
    "PiCtBaFi02start.vm",                          # efftis 入口
    "pubGroupTop.do",                              # efftis 入口
    "PPUBC00100",                                  # efftis/PPUBC 入口
    "EjPPIj",                                      # e-Aichi/supercals 入口
)


def is_generic_url(url: str) -> bool:
    """個別案件ではなく検索トップ等に着地する汎用URLか。"""
    return bool(url) and any(g in url for g in GENERIC_URL_MARKERS)


def is_real_link(url: str) -> bool:
    """実在の個別ページとして使えるURLか（http かつ 汎用/ダミーでない）。"""
    return (bool(url) and url.startswith("http")
            and "example.com" not in url and not is_generic_url(url))


def classify_platform(domain: str) -> str:
    """発注機関ドメインから使用する電子入札システム名を判定する。

    判定できない（＝誤データ・個別サイト）ものは "個別/不明" を返し、
    呼び出し側はポータルURLを出さない（誤誘導しないため）。
    """
    d = (domain or "").lower()
    rules = [
        ("i-ppi.jp", "統合PPI"),
        ("efftis.jp", "efftis"),
        ("e-procurement.metro.tokyo", "東京都電子調達"),
        ("e-tokyo.lg.jp", "東京電子自治体共同運営"),
        ("e-aichi", "e-Aichi"),
        ("e-harp", "e-harp"),
        ("p-portal.go.jp", "GEPS"),
        ("e-bisc.go.jp", "e-bisc"),
        ("bit.courts.go.jp", "裁判所BIT"),
        ("kumamoto-idc", "PPI_P"),
        ("dennyu.pref.kagawa", "PPI_P"),
        ("keiyaku.city.hiroshima", "PPI_P"),
        ("cals", "電子入札コアシステム"),
    ]
    for key, name in rules:
        if key in d:
            return name
    return "個別/不明"


# 大手・安定の電子入札システムだけ、正規の入口URLを手当て。
# ここに載るものは「申し込み先」として確実なものに限る。per-instance（機関ごとに
# URLが違う）系は載せず、検索導線に委ねる（誤URLを出さない方針）。
KNOWN_PORTALS: dict[str, tuple[str, str, str]] = {
    # key: (システム表示名, 入口URL, 補足)
    "GEPS": ("政府電子調達システム（GEPS）", "https://www.p-portal.go.jp/",
             "国の機関。事前に「全省庁統一資格」の取得が必要です。"),
    "東京都電子調達": ("東京都電子調達システム", "https://www.e-procurement.metro.tokyo.lg.jp/",
                  "東京都の競争入札参加資格の登録が必要です。"),
    "e-Aichi": ("あいち電子調達システム（CALS/EC）", "https://www.chotatsu.e-aichi.jp/",
                "愛知県・県内市町村の入札参加資格登録が必要です。"),
    "e-harp": ("北海道電子入札システム e-harp", "https://www.e-harp.jp/",
               "北海道・道内自治体の入札参加資格登録が必要です。"),
    "統合PPI": ("入札情報サービス PPI（i-ppi.jp）", "https://www.i-ppi.jp/",
              "発注機関ごとの入札参加資格登録が必要です。"),
    "裁判所BIT": ("裁判所 電子入札システム（BIT）", "https://www.bit.courts.go.jp/",
               "裁判所の競争入札参加資格の登録が必要です。"),
    "e-bisc": ("独立行政法人 電子調達システム（e-bisc）", "https://www.e-bisc.go.jp/",
               "対象独法の入札参加資格登録が必要です。"),
}


def portal_for(domain: str) -> tuple[str, str, str] | None:
    """ドメインから確実な電子入札ポータル(name, url, note)を返す。無ければ None。"""
    return KNOWN_PORTALS.get(classify_platform(domain))


def _clean_title(title: str) -> str:
    """検索精度を上げるため、タイトル末尾の "（PDF 221.2 KB）" 等を除去する。"""
    return re.sub(r"[（(]\s*PDF[^）)]*[）)]\s*$", "", title or "").strip()


def _search_url(*terms: str) -> str:
    """与えた語でのウェブ検索URL（常に最新・保守不要の確実な導線）。"""
    q = " ".join(t for t in terms if t)
    return "https://www.google.com/search?q=" + urllib.parse.quote(q)


def notice_search_url(title: str, agency: str = "") -> str:
    """その案件の公告ページをウェブで探すための検索URL。"""
    return _search_url(_clean_title(title), agency)


def register_search_url(prefecture: str, agency: str = "") -> str:
    """入札参加資格の登録方法を探すための検索URL。"""
    who = agency or prefecture
    return _search_url(who, "電子入札", "入札参加資格", "登録")


def application_guide(case: dict, agency_info: dict | None) -> dict:
    """案件1件に対する応募導線を組み立てて返す（テンプレートが使う dict）。

    返すキー:
      notice_url        … この案件の公告（実リンク, 無ければ None）
      notice_search_url … 公告をウェブで探す検索URL（常に有る）
      spec_url          … 設計図書の実リンク（無ければ None）
      portal            … (システム名, URL, 補足) 確実な大手ポータルのみ。無ければ None
      register_search   … 入札参加資格 登録方法の検索URL（常に有る）
    """
    detail = (case.get("detail_url") or "")
    spec = (case.get("spec_url") or "")
    domain = (agency_info or {}).get("domain", "")
    return {
        "notice_url": detail if is_real_link(detail) else None,
        "notice_search_url": notice_search_url(case.get("title", ""), case.get("agency", "")),
        "spec_url": spec if is_real_link(spec) else None,
        "portal": portal_for(domain),
        "register_search": register_search_url(case.get("prefecture", ""),
                                               (agency_info or {}).get("name", "")),
    }
