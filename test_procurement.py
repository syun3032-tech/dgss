"""procurement.py の回帰テスト（追加依存なし・AI不使用）。

`python test_procurement.py` で実行。応募導線ロジックが壊れたら気付けるようにする。
これが「コードで自動的に正しさを担保する」仕組みの一部。
"""

from __future__ import annotations

import procurement as p


def _check(name: str, cond: bool) -> bool:
    print(("  OK " if cond else "FAIL") + "  " + name)
    return cond


def test_generic_url_detection() -> bool:
    ok = True
    ok &= _check("調達ポータル検索トップは汎用",
                 p.is_generic_url("https://www.p-portal.go.jp/pps-web-biz/UAA01/OAA0101"))
    ok &= _check("PPI検索トップは汎用",
                 p.is_generic_url("https://www.i-ppi.jp/IPPI/SearchServices/Web/Index.htm"))
    ok &= _check("実PDFは汎用でない",
                 not p.is_generic_url("https://www.city.suita.osaka.jp/foo.pdf"))
    ok &= _check("空文字は汎用でない", not p.is_generic_url(""))
    return ok


def test_is_real_link() -> bool:
    ok = True
    ok &= _check("実httpリンクは有効", p.is_real_link("https://example.go.jp/koukoku.pdf"))
    ok &= _check("example.comダミーは無効", not p.is_real_link("http://example.com/x"))
    ok &= _check("汎用URLは無効",
                 not p.is_real_link("https://www.p-portal.go.jp/pps-web-biz/UAA01/OAA0101"))
    ok &= _check("空は無効", not p.is_real_link(""))
    return ok


def test_classify_platform() -> bool:
    ok = True
    ok &= _check("東京都 e-procurement → 東京都電子調達",
                 p.classify_platform("www.e-procurement.metro.tokyo.lg.jp") == "東京都電子調達")
    ok &= _check("p-portal → GEPS", p.classify_platform("www.p-portal.go.jp") == "GEPS")
    ok &= _check("e-aichi → e-Aichi", p.classify_platform("www.chotatsu.e-aichi.jp") == "e-Aichi")
    ok &= _check("cals → 電子入札コアシステム",
                 p.classify_platform("ppi.cals-ibaraki.lg.jp") == "電子入札コアシステム")
    # 誤データ（競輪サイト）は判定できず "個別/不明" になり、ポータルを出さない
    ok &= _check("競輪サイト(誤データ)は個別/不明",
                 p.classify_platform("www.keirinwa.com") == "個別/不明")
    return ok


def test_portal_for() -> bool:
    ok = True
    portal = p.portal_for("www.p-portal.go.jp")
    ok &= _check("GEPSドメインで正規ポータルが返る", portal is not None and portal[1] == "https://www.p-portal.go.jp/")
    # 誤データ・個別ドメインは None（誤誘導しない）
    ok &= _check("誤データドメインは None", p.portal_for("www.keirinwa.com") is None)
    ok &= _check("不明ドメインは None", p.portal_for("") is None)
    return ok


def test_search_urls() -> bool:
    ok = True
    u = p.notice_search_url("吹田市立自然の家大規模改修工事（PDF 221.2 KB）", "大阪府吹田市")
    ok &= _check("公告検索URLはgoogle検索", u.startswith("https://www.google.com/search?q="))
    ok &= _check("PDFノイズが除去される", "PDF" not in p._clean_title("工事名（PDF 221.2 KB）"))
    r = p.register_search_url("大阪府", "吹田市役所")
    ok &= _check("登録検索URLに『入札参加資格』が含まれる", "%E5%85%A5%E6%9C%AD" in r or "入札参加資格" in r)
    return ok


def test_application_guide() -> bool:
    ok = True
    # 実リンクを持つ案件: notice_url が出て、汎用ではない
    case_real = {"title": "受変電設備工事", "agency": "大阪府吹田市", "prefecture": "大阪府",
                 "detail_url": "https://www.city.suita.osaka.jp/x.pdf", "spec_url": ""}
    g = p.application_guide(case_real, {"domain": "", "name": "吹田市役所"})
    ok &= _check("実リンク案件は notice_url が出る", g["notice_url"] is not None)
    ok &= _check("検索フォールバックは常にある", bool(g["notice_search_url"]))

    # 汎用URLしかない案件: notice_url は None（誤誘導しない）、検索で補う
    case_generic = {"title": "明石警察署受変電工事", "agency": "兵庫県", "prefecture": "兵庫県",
                    "detail_url": "https://www.p-portal.go.jp/pps-web-biz/UAA01/OAA0101", "spec_url": ""}
    g2 = p.application_guide(case_generic, None)
    ok &= _check("汎用URL案件は notice_url=None", g2["notice_url"] is None)
    ok &= _check("汎用URL案件でも検索URLはある", bool(g2["notice_search_url"]))

    # GEPSドメインの機関: portal が出る
    g3 = p.application_guide(case_real, {"domain": "www.p-portal.go.jp", "name": "防衛省"})
    ok &= _check("GEPS機関は portal が出る", g3["portal"] is not None)
    return ok


def main() -> int:
    tests = [
        test_generic_url_detection, test_is_real_link, test_classify_platform,
        test_portal_for, test_search_urls, test_application_guide,
    ]
    all_ok = True
    for t in tests:
        print(f"\n[{t.__name__}]")
        all_ok &= t()
    print("\n" + ("=== 全テストPASS ===" if all_ok else "=== 失敗あり ==="))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
