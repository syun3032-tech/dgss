"""検品ロジック（data_expectations）の回帰テスト。ネット不要・追加依存なし。

実行:
  .venv/bin/python test_data_expectations.py

守りたいこと（2026-08-17の事故の再発防止）:
  - 主力ソースが0件になったら**必ず重大扱い**になること
  - 件数が足りていても、主力ソースの公告が古ければ気づけること
    （＝古い案件で件数だけ埋まっている、一番わかりにくい壊れ方）
  - 正常なデータで誤報を出さないこと（狼少年になるとバナーが無視される）
"""

from __future__ import annotations

import datetime
import sqlite3

import data_expectations as dx

_TODAY = datetime.date.today()

# 各テストの前に基準値(data_baseline.json)を「無し」に固定する。
# これをしないと、本番運用で基準値ファイルが育ったとたんテストが落ち始める
# （テスト用の少ない件数が「前回比で急減」と判定されるため）。
# pytest は setup_function を、直接実行は _run_all がこれを呼ぶ。
_REAL_LOAD_BASELINE = dx.load_baseline


def setup_function(_fn=None):
    dx.load_baseline = lambda mode: {}


def _iso(days_ago: int) -> str:
    return (_TODAY - datetime.timedelta(days=days_ago)).isoformat()


def _db(rows: list[tuple[str, str]]) -> sqlite3.Connection:
    """(source, announced_date) のリストから、検品に必要な最小のDBを作る。"""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE cases (source TEXT, announced_date TEXT)")
    c.executemany("INSERT INTO cases VALUES (?, ?)", rows)
    return c


def _healthy(n_main: int = 9000) -> list[tuple[str, str]]:
    """正常なデータ（主力ソースが十分な件数・公告も新しい）。"""
    rows = [("官公需API", _iso(1)) for _ in range(n_main)]
    rows += [("調達ポータル落札実績", _iso(2)) for _ in range(200)]
    return rows


def test_healthy_data_has_no_findings():
    """正常なら指摘0件（誤報を出さない）。"""
    fs = dx.inspect(_db(_healthy()), full=False)
    assert fs == [], f"正常データで誤報: {[str(f) for f in fs]}"


def test_main_source_zero_is_critical():
    """主力ソースが0件＝重大。ここが今回の事故そのもの。"""
    # 他ソースだけで件数を稼いだ状態（本番が1,879件になっていた形）
    rows = [("PPI", _iso(1)) for _ in range(6000)]
    fs = dx.inspect(_db(rows), full=False)
    assert any(f.critical for f in fs), "主力ソース0件が重大として検知されない"
    assert any("官公需API" in f.message for f in fs), "どのソースが死んだのか分からない"


def test_thin_full_build_is_blocked():
    """--full で痩せたDB（1,879件相当）は重大＝公開させない。"""
    rows = _healthy(n_main=1_600)
    fs = dx.inspect(_db(rows), full=True)
    assert any(f.critical for f in fs), "痩せた網羅DBが合格してしまう（本番へ配られる）"


def test_stale_main_source_detected_even_with_enough_rows():
    """件数は足りているのに主力の公告が古い＝重大。一番わかりにくい壊れ方。"""
    rows = [("官公需API", _iso(30)) for _ in range(9000)]
    fs = dx.inspect(_db(rows), full=False)
    assert any(f.critical and "官公需API" in f.message for f in fs), \
        "古いデータで件数だけ埋まっている状態を見逃している"


def test_weekend_does_not_cause_false_alarm():
    """金曜公告を月曜に見る（3日前）程度では警告しない＝狼少年にしない。"""
    rows = [("官公需API", _iso(3)) for _ in range(9000)]
    rows += [("調達ポータル落札実績", _iso(3)) for _ in range(200)]
    fs = dx.inspect(_db(rows), full=False)
    assert fs == [], f"3日前の公告で誤報: {[str(f) for f in fs]}"


def _with_baseline(mode: str, total: int, sources: dict[str, int]):
    """load_baseline を差し替えるコンテキスト（ファイルを汚さない）。"""
    class _Ctx:
        def __enter__(self):
            self.orig = dx.load_baseline
            dx.load_baseline = lambda m: (
                {"updated": "2026-08-01", "total": total, "sources": sources}
                if m == mode else {})
            return self

        def __exit__(self, *a):
            dx.load_baseline = self.orig
    return _Ctx()


def test_silent_halving_is_caught_even_above_floor():
    """下限は超えているのに前回の半分＝重大。絶対値だけでは見逃す「静かな劣化」。"""
    # 前回 13,000件 → 今回 6,000件（--fast の下限 5,000 は超えている）
    rows = _healthy(n_main=5_800)
    with _with_baseline("fast", 13_200, {"官公需API": 13_000}):
        fs = dx.inspect(_db(rows), full=False)
    assert any(f.critical for f in fs), "前回比の急減を見逃している"
    assert any("急減" in f.message or "減少" in f.message for f in fs)


def test_small_dip_is_only_a_warning():
    """1〜2割の増減は季節変動もある。重大にせず注意に留める（狼少年にしない）。"""
    rows = _healthy(n_main=9_000)
    with _with_baseline("fast", 11_500, {"官公需API": 11_300}):
        fs = dx.inspect(_db(rows), full=False)
    assert not any(f.critical for f in fs), f"2割減で止めてしまう: {[str(f) for f in fs]}"


def test_no_baseline_means_no_drop_findings():
    """基準値がまだ無い初回は、比較による指摘を出さない。"""
    with _with_baseline("fast", 0, {}):
        fs = dx.inspect(_db(_healthy()), full=False)
    assert fs == [], f"基準値なしで誤報: {[str(f) for f in fs]}"


def test_report_always_states_counts():
    """「異常なし」だけで終わらせず、必ず件数を添える（検品の報告ルール）。"""
    conn = _db(_healthy())
    text = dx.format_report(dx.inspect(conn, full=False), counts=dx.source_counts(conn))
    assert "官公需API" in text and "指摘 0 件" in text, f"報告に件数が無い:\n{text}"


def test_every_expect_has_a_reason():
    """期待値には必ず根拠(why)を書く。数字だけ残ると誰も直せなくなる。"""
    for e in dx.EXPECT:
        assert e.why.strip(), f"{e.name} の期待値に根拠(why)が無い"
        assert e.min_full >= e.min_fast, f"{e.name}: --full の下限が --fast より緩い"


def _run_all():
    tests = [v for n, v in sorted(globals().items())
             if n.startswith("test_") and callable(v)]
    for t in tests:
        setup_function(t)
        t()
        print(f"  ok  {t.__name__}")
    dx.load_baseline = _REAL_LOAD_BASELINE
    print(f"\n{len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
