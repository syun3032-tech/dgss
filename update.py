"""データ更新（ローカル実行・ランニングコスト 0 円）。

電気工事入札の最新案件を各無料ソースから取得して SQLite に取り込む。
**有料API・クラウド・サブスク一切なし**。使うのは Playwright（無料）と SQLite（無料）だけ。
ローカルPCで実行するだけなので、実行している間のCPU以外コストはかからない。

使い方:
  python update.py            … 関西サンプル＋PPI近畿(実データ)を取り込み（追記）
  python update.py --reset    … 既存案件をクリアしてから取り込み（関西中心に作り直し）
  python update.py --koukai 茨城県   … 指定自治体の入札情報公開システムも取り込み

定期実行（これも無料）:
  - macOS: launchd か `crontab -e` で1日1回など。例（毎朝7時）:
      0 7 * * * cd /path/to/kawano-njss-modoki && .venv/bin/python update.py --reset >> update.log 2>&1
  - クラウドのcronサービス等は不要。PCが起動している時間に回せばよい。
"""

from __future__ import annotations

import sys

import db
import ppi_scraper
import seed_data

# --fast（毎日の自動更新／Renderビルド時）で、これ未満なら生成失敗とみなしデプロイ中止。
# 通常は官公需APIだけで数千件入るので、500未満はAPI障害等の異常値。
FAST_MIN_CASES = 500


def run(reset: bool = False, koukai_instances: list[str] | None = None,
        with_samples: bool = False, fast: bool = False) -> None:
    # fast=True: 官公需API＋監視機関のみ（HTTPのみ・Playwright不要）＝毎日の自動更新向けで高速・堅牢。
    db.init_db()
    if reset and not fast:
        removed = db.clear_cases()
        print(f"[reset] 既存案件 {removed} 件をクリア")

    # 0) 官公需情報ポータルAPI（中小企業庁）— 国・地方・独法を全国横断集約した公式・無料API。
    #    これが主力ソース（HTTPのみ・全国・仕様書添付つき）。関西を厚く取ってから全国。
    # 【フェイルセーフ】先に取得してから、成功(>0件)した時だけ差し替える。
    #   APIが落ちている時に既存データを消さない。fast時はAPI分だけ入替（PPI/自治体は保持）。
    import kkj_scraper
    try:
        # 関西は電気の工事＋役務を横断で厚く取る（役務=保安管理/保守点検の取りこぼし防止）
        rows = kkj_scraper.fetch_kansai_electrical()
        # 全国は電気系クエリを横断（受変電/照明/太陽光/保安管理 等）で取りこぼし防止。
        # 旧実装は単一クエリ"電気工事"・Count1000で頭打ち→近畿以外を大量に落としていた。
        rows += kkj_scraper.fetch_nationwide_electrical()
        # external_id で重複排除（関西電気と全国の重なりを除く）
        rows = list({r["external_id"]: r for r in rows if r.get("title")}.values())
        if rows:
            if fast:
                db.clear_cases("官公需API")  # 成功時のみ古いAPI行を入替
            n = db.upsert_cases(rows)
            print(f"[官公需API] {n} 件（関西＋全国・重複除外）")
        else:
            print("[官公需API] 取得0件のため既存データを維持（差し替えなし）")
    except Exception as e:  # noqa: BLE001
        print(f"[官公需API] 失敗・既存データ維持: {str(e)[:70]}")

    if fast:
        # 高速モード：監視機関だけ足して終了（Playwrightは使わない）
        try:
            import agency_import
            n_ag = agency_import.load()
            print(f"[監視機関リスト] {n_ag} 機関")
            if n_ag == 0:
                # 致命ではない（案件は出る）が、応募ガイドのポータル判定や監視機関ページが
                # 空になるためビルドログで気付けるよう目立たせる。スプシ非公開化が典型原因。
                print("[警告] 監視機関が0件です。Googleスプレッドシートが非公開/移動の"
                      "可能性があります（応募ガイドのポータル判定が弱くなります）。")
        except Exception as e:  # noqa: BLE001
            print(f"[警告] 監視機関リスト取得失敗: {str(e)[:70]}")
        n_cases = db.count_cases()
        print(f"=== 高速更新完了: 案件 {n_cases} 件 / 監視機関 {db.count_agencies()} 機関 ===")
        # 【安全弁】案件が極端に少ない＝官公需APIが落ちていた等で生成失敗。
        # この状態でデプロイすると「空っぽのサイト」が公開されてしまうため、
        # 非0終了でビルドを止める→Renderは直前の正常デプロイを維持する。
        if n_cases < FAST_MIN_CASES:
            print(f"[安全弁] 案件 {n_cases} 件は下限 {FAST_MIN_CASES} 未満。"
                  "データ生成に失敗とみなしビルドを中止（前回の正常デプロイを維持）。")
            sys.exit(1)
        return

    # 1) 自治体の電気工事（実データ・個別システム）— 京都府・愛知県・堺市(大阪府)
    for mod_name, label in [("kyoto_scraper", "京都府"), ("aichi_scraper", "愛知県(e-Aichi)")]:
        try:
            mod = __import__(mod_name)
            n = mod.load()
            print(f"[{label} 自治体・実データ] {n} 件")
        except Exception as e:  # noqa: BLE001
            print(f"[{label}] 取得失敗（スキップ）: {str(e)[:80]}")
    # 関西を厚く：PPUBC系の自治体（堺市・明石市など。INSTANCESにbase追加で拡張可）
    try:
        import ppubc_scraper
        for inst in ppubc_scraper.INSTANCES:
            try:
                n = ppubc_scraper.load(inst)
                print(f"[{inst}(PPUBC) 自治体・実データ] {n} 件")
            except Exception as e:  # noqa: BLE001
                print(f"[{inst}(PPUBC)] 取得失敗（スキップ）: {str(e)[:60]}")
    except Exception as e:  # noqa: BLE001
        print(f"[PPUBC] モジュール読込失敗: {str(e)[:60]}")

    # 1b) サンプル（既定OFF。--with-samples で関西の見本データを足す）
    if with_samples:
        n_sample = seed_data.seed()
        print(f"[sample] 関西サンプル {n_sample} 件")

    # 2) PPI（i-ppi.jp）— 全国9地方の国の機関 電気設備工事 入札の経過（実データ）
    #    全地方をループして全国をカバー。落札者(競合)も少数ずつ取得する。
    DISTRICTS = ["北海道", "東北", "関東", "北陸", "中部", "近畿", "中国", "四国", "九州・沖縄"]
    ppi_total = 0
    for dist in DISTRICTS:
        try:
            rows = ppi_scraper.fetch_live(
                keika=True, kikan="国の機関", district=dist,
                koji_kbn="電気設備工事", count="100",
                with_winner=True, max_detail=4,  # 各地方 落札者を少数取得
            )
            norm = [ppi_scraper._normalize(r) for r in rows]
            n = db.upsert_cases(norm) if norm else 0
            ppi_total += n
            print(f"[PPI {dist}] {n} 件")
        except Exception as e:  # noqa: BLE001
            print(f"[PPI {dist}] 取得失敗（スキップ）: {str(e)[:80]}")
    print(f"[PPI全国 合計] {ppi_total} 件")

    # 3) 自治体の入札情報公開システム（任意・登録があるインスタンスのみ）
    for inst in (koukai_instances or []):
        try:
            import koukai_scraper
            n = koukai_scraper.load(inst, count="100", max_pages=2)
            print(f"[自治体 {inst}] {n} 件")
        except Exception as e:  # noqa: BLE001
            print(f"[自治体 {inst}] 取得失敗（スキップ）: {str(e)[:80]}")

    # 4) 監視対象の発注機関リスト（Googleスプレッドシート）を取り込み
    try:
        import agency_import
        na = agency_import.load()
        print(f"[監視機関リスト] {na} 機関")
    except Exception as e:  # noqa: BLE001
        print(f"[監視機関リスト] 取得失敗（スキップ）: {str(e)[:80]}")

    print(f"=== 更新完了: 案件 {db.count_cases()} 件 / 監視機関 {db.count_agencies()} 機関 ===")


if __name__ == "__main__":
    reset = "--reset" in sys.argv
    with_samples = "--with-samples" in sys.argv
    insts: list[str] = []
    if "--koukai" in sys.argv:
        i = sys.argv.index("--koukai")
        insts = [a for a in sys.argv[i + 1:] if not a.startswith("--")]
    run(reset=reset, koukai_instances=insts, with_samples=with_samples,
        fast="--fast" in sys.argv)
