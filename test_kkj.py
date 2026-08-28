"""kkj_scraper の回帰テスト（追加依存なし）。

実行:
  .venv/bin/python test_kkj.py      # 単体（pytest不要）
  .venv/bin/pytest test_kkj.py      # pytestがあれば

目的（再現性の担保）:
  - 全国取得が「単一クエリ・1000件頭打ち」へ退行していないこと
    （fetch_nationwide_electrical が電気クエリを全国横断＋dedupする）を固定する。
  - 1000万超フィルタが依存する予定価格抽出（parse_budget_from_text）を固定する。
  - 締切抽出の基本挙動を固定する。
"""

from __future__ import annotations

import kkj_scraper as k


# ============================================================
# テスト用のダブル：fetch をモックして API を叩かせない
# ============================================================

def _install_fake_fetch(monkeypatch_target):
    """kkj_scraper.fetch を、呼び出しを記録する偽実装に差し替える。

    戻り値: 呼び出しログ list[(query, category, lg_codes)]
    各クエリは external_id がクエリ名に紐づく1件を返す（dedup検証用に一部重複させる）。
    """
    calls: list[tuple] = []

    def fake_fetch(query="電気工事", category="2", lg_codes=None, count=1000, timeout=40):
        calls.append((query, category, tuple(lg_codes) if lg_codes else None))
        # クエリ名で一意な1件＋全クエリ共通の重複1件（dedup効果を見るため）
        return [
            {"external_id": f"KKJ-{query}", "title": f"{query}案件", "category": "電気工事-電気設備"},
            {"external_id": "KKJ-DUP", "title": "重複案件", "category": "電気工事-電気設備"},
        ]

    k.fetch = fake_fetch  # type: ignore[assignment]
    return calls


def test_nationwide_uses_all_electrical_queries_nationwide():
    """全国取得は電気クエリ群を lg_codes 無し(全国)で横断し、dedupする。"""
    orig = k.fetch
    try:
        calls = _install_fake_fetch(None)
        rows = k.fetch_nationwide_electrical()

        queried = [c[0] for c in calls]
        # 工事(Cat2)の電気クエリが全て使われている（"電気工事"単一に退行していない）
        for q in k.ELEC_QUERIES:
            assert q in queried, f"工事クエリ {q} が全国取得で使われていない"
        # 役務(Cat3)は電気役務に限定（広いSERVICE_QUERIESを全国に流していない）
        for q in k.ELEC_SERVICE_QUERIES:
            assert q in queried, f"電気役務クエリ {q} が使われていない"
        assert "塗装" not in queried and "清掃" not in queried, "全国に広い非電気役務が漏れている"
        # 全クエリが全国（lg_codes=None）で呼ばれている
        assert all(c[2] is None for c in calls), "全国取得なのに都道府県限定で呼ばれている"
        # category は Cat2 と Cat3 の両方が使われている
        cats = {c[1] for c in calls}
        assert cats == {"2", "3"}, f"想定カテゴリで呼ばれていない: {cats}"
        # dedup: KKJ-DUP は1件に集約される
        ids = [r["external_id"] for r in rows]
        assert ids.count("KKJ-DUP") == 1, "external_id の重複排除が効いていない"
        # ユニークなクエリ名ぶんの案件＋共通1件（受変電/電気主任技術者は両群に重複）
        expected = len(set(k.ELEC_QUERIES) | set(k.ELEC_SERVICE_QUERIES)) + 1
        assert len(rows) == expected, f"件数が想定外: {len(rows)} != {expected}"
    finally:
        k.fetch = orig


def test_fetch_retry_recovers_from_transient_error():
    """_fetch_retry は一過性失敗を再試行し、最終的に成功すれば結果を返す。"""
    orig = k.fetch
    try:
        state = {"n": 0}

        def flaky(query="", category="2", lg_codes=None, count=1000, timeout=40):
            state["n"] += 1
            if state["n"] == 1:
                raise TimeoutError("一過性")
            return [{"external_id": "KKJ-OK", "title": "ok"}]

        k.fetch = flaky  # type: ignore[assignment]
        # sleepを潰して高速化
        import time as _t
        orig_sleep = _t.sleep
        _t.sleep = lambda *_a, **_k: None
        try:
            out = k._fetch_retry("電気工事", "2", retries=2)
        finally:
            _t.sleep = orig_sleep
        assert out and out[0]["external_id"] == "KKJ-OK", "リトライ後の成功を拾えていない"
        assert state["n"] == 2, "リトライ回数が想定外"
    finally:
        k.fetch = orig


def test_ssl_context_bundles_kkj_intermediate():
    """kkj.go.jp 用の中間CAが同梱され、検証コンテキストに載っていること。

    2026-07-07〜08-16、サーバが誤った中間CAを送るせいで検証に失敗し、官公需APIの
    取得が全滅していた（本番DBが 1,879 件まで痩せた）。同梱CAが消えると同じ事故に
    戻るため、存在と読み込みを固定する。ネットには触れないオフラインテスト。
    """
    assert k._CA_BUNDLE.exists(), f"同梱の中間CAが見つからない: {k._CA_BUNDLE}"
    subjects = [dict(x for t in c["subject"] for x in t).get("commonName", "")
                for c in k._ssl_context().get_ca_certs()]
    assert "JPRS DV RSA CA 2024 G1" in subjects, \
        "kkj.go.jp のサーバ証明書を発行した中間CAが信頼ストアに載っていない"
    # 検証を切る回避（CERT_NONE）へ退行していないこと＝中間者攻撃を許さない
    ctx = k._ssl_context()
    assert ctx.verify_mode == __import__("ssl").CERT_REQUIRED, "証明書検証が無効化されている"
    assert ctx.check_hostname is True, "ホスト名検証が無効化されている"


def test_fetch_retry_records_reason_on_total_failure():
    """全リトライ失敗時、理由を last_error() に残すこと（無言の0件を作らない）。"""
    orig = k.fetch
    try:
        def always_fail(query="", category="2", lg_codes=None, count=1000, timeout=40):
            raise OSError("証明書エラー想定")

        k.fetch = always_fail  # type: ignore[assignment]
        import time as _t
        orig_sleep, _t.sleep = _t.sleep, lambda *_a, **_k: None
        try:
            out = k._fetch_retry("電気工事", "2", retries=1)
        finally:
            _t.sleep = orig_sleep
        assert out == [], "失敗時は空リストを返すべき"
        assert "証明書エラー想定" in k.last_error(), "失敗理由が記録されていない"
    finally:
        k.fetch = orig


def test_parse_budget_picks_max_yen():
    """予定価格抽出：本文から円を数値化し、複数あれば最大を採る（1000万フィルタの土台）。"""
    yen, txt = k.parse_budget_from_text("予定価格 12,300,000円 ほか 参考価格 9,000,000円")
    assert yen == 12_300_000, f"最大額を採れていない: {yen}"
    assert txt == "12,300,000円"
    # 妥当域外（10万未満）は採らない
    assert k.parse_budget_from_text("手数料 50,000円")[0] == 0
    # 抽出不能は (0, "")
    assert k.parse_budget_from_text("金額の記載なし") == (0, "")


def test_parse_deadline_reiwa_and_keyword_priority():
    """締切抽出：提出期限(令和)を西暦ISOへ。年無しM月D日は推測しない。"""
    iso = k.parse_deadline_from_text("入札書提出期限 令和8年6月30日 開札 令和8年7月1日")
    assert iso == "2026-06-30", f"提出期限を優先できていない: {iso}"
    # 年が特定できない裸の日付は採らない（誤締切より空）
    assert k.parse_deadline_from_text("締切は6月30日") == ""


def test_parse_deadline_scans_all_occurrences():
    """見出しの「提出期限」に空振りしても、後続の実日付を拾える（全出現探索）。"""
    text = ("５ 入札書の提出期限及び場所\n"
            "(1) 提出期限 電子調達システムにより令和8年7月15日まで")
    assert k.parse_deadline_from_text(text, "2026-06-20") == "2026-07-15"


def test_parse_deadline_rejects_out_of_range():
    """公告日と同日・公告前・現実離れした遠い先（工期末等）は締切として採らない。"""
    # 同日は不可（入札締切が公告当日はあり得ない）
    assert k.parse_deadline_from_text("提出期限 令和8年6月20日", "2026-06-20") == ""
    # 公告より前は不可
    assert k.parse_deadline_from_text("提出期限 令和8年6月1日", "2026-06-20") == ""
    # 150日超（工期末の誤抽出想定）は不可
    assert k.parse_deadline_from_text("提出期限 令和9年3月26日", "2026-06-20") == ""
    # 範囲内は採る
    assert k.parse_deadline_from_text("提出期限 令和8年7月10日", "2026-06-20") == "2026-07-10"


# ============================================================
# 業種分類（classify_category）
# ============================================================
# 2026-08-28 川野さん指摘: 京都市「公衆トイレ応急対応業務委託」（種目=清掃）が
# 「電気工事-照明」として電気の一覧に出ていた。原因は、官公需APIの説明文に
# 添付の仕様書・点検表まで丸ごと入るため、そこにある「照明センサー点検」
# 「照明の電球が切れていないか」を本文フォールバックが拾っていたこと。
# 実データ（京都市 入札番号434866・9,751字）の要点を縮約して固定する。

_TOILET_DESC = (
    "公衆トイレ応急対応業務委託 入札公告 公告日：2026.08.17 入札番号 434866 "
    "案件名称 公衆トイレ応急対応業務委託 予定価格（税抜き） 6,300,000円 "
    "開札日 2026.08.25 種目 清掃 内容 その他（清掃） "
    "要求課 環境政策局 循環型社会推進部 まち美化推進課 "
    "入札参加資格(その他) 公衆トイレの清掃業務の実績があること。\n"
    "仕様書 ２ 委託の範囲⑴ 清掃点検及び簡易清掃 対象の公衆トイレの清掃状況を点検し、"
    "不具合がある場合、清掃等を実施する。⑵ 設備点検及び簡易修繕 給水設備、排水桝、"
    "建具等を点検し、【業務の範囲】給水設備の調整及び交換、漏水防止、吐水の流量調整、"
    "照明センサー点検、建具等設備部材の調整、電球交換等\n"
    "(別紙2)応急対応業務点検表 照明 照明の電球が切れていないか ✔ "
    "清掃トイレ名：1 清掃トイレ名：2 清掃トイレ名：3"
)


def test_classify_reads_declared_category_field():
    """公告が明記する「種目」欄を、付属仕様書の付随記述より優先して読む。"""
    got = k.classify_category(_TOILET_DESC, title="公衆トイレ応急対応業務委託")
    assert got == "清掃・廃棄物", f"種目=清掃 を読めていない: {got}"
    assert not k.is_electrical(got), "清掃委託が電気の一覧に出てしまう"


def test_body_guard_yields_when_document_is_clearly_another_trade():
    """種目欄が無くても、文書全体が別業種なら付随の電気語で電気にしない。"""
    text = ("庁舎清掃業務委託 清掃の範囲 日常清掃、定期清掃、床面清掃、"
            "ガラス清掃、清掃用具は受注者が用意する。"
            "点検表: 照明の電球が切れていないか。")
    got = k.classify_category(text, title="令和8年度 庁舎維持管理業務委託")
    assert got == "清掃・廃棄物", f"清掃が主題の文書を電気にしている: {got}"


def test_incidental_non_electrical_mention_does_not_flip():
    """一度出るだけの「清掃」等では電気判定を覆さない（誤爆ガードの閾値）。"""
    text = ("交通信号機改良工事 工事概要 制御機更新、配電線路工、"
            "電気設備工事一式。施工後は現場の清掃を行うこと。")
    got = k.classify_category(text, title="西新町１丁目ほか４か所交通信号機改良工事")
    assert got == "電気工事-電気設備", f"本文からの電気判定が失われた: {got}"


def test_declared_field_ignored_when_it_maps_to_nothing():
    """種目が「建物管理」等どの業種にも当たらないときは決め打ちせず本文へ落とす。"""
    text = ("庁舎維持管理業務 種目 建物管理 内容 その他 "
            "受変電設備の年次点検、キュービクルの点検を行う。")
    got = k.classify_category(text, title="令和8年度 庁舎維持管理業務")
    assert got == "電気工事-受変電", f"本文の電気判定に落とせていない: {got}"


def test_title_still_wins_over_declared_field():
    """案件名で判定できるときは従来どおり案件名を採る（順序の退行防止）。"""
    text = "種目 清掃 内容 その他（清掃） 照明設備の更新を行う。"
    got = k.classify_category(text, title="○○小学校 照明設備更新工事")
    assert got == "電気工事-照明", f"案件名優先が崩れている: {got}"


def _run_all():
    tests = [v for n, v in sorted(globals().items())
             if n.startswith("test_") and callable(v)]
    passed = 0
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
        passed += 1
    print(f"\n{passed}/{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
