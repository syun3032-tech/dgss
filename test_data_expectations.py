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
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)}/{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
