"""SQLite データ層（独立ツール用・外部依存なし）。

NJSS無双君 が Supabase を必要とするのに対し、本ツールは
ローカル SQLite だけで完結させる（鍵設定不要・すぐ動く）。

主要関数:
  - init_db()             … スキーマ作成
  - upsert_cases(rows)    … 案件を一括投入（external_id で重複排除）
  - list_cases(...)       … 地方/都道府県/業種/仕様書状態/キーワードで絞り込み
  - get_case(case_id)     … 1件取得
  - distinct_values(col)  … フィルタUI用の候補値
  - count_cases()         … 件数
"""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

import supa  # Supabase永続化（入力データをデプロイ揮発から守る・未設定時は無効）

DB_PATH = Path(__file__).parent / "denki_bid.db"

# Supabaseからの復元中は write-through を止める（書き戻しの無限ループ防止）
_restoring = False

# 【重要・データ消失防止】Supabaseから読み込んだが、案件(cases)側に該当が無くて
# SQLiteへ復元できなかった申請。**捨てずにここへ退避する。**
#
# なぜ必要か（2026-08-17の事故で判明）:
#   Renderは毎デプロイでSQLiteが作り直され、起動時にSupabaseから申請を復元する。
#   その際 external_id で今の案件IDに解決するが、**案件データ側が一時的に壊れて
#   いると解決できず、従来はその申請を黙って捨てていた**。
#   一方 _push_applications() は「今SQLiteにある全件」でSupabaseを丸ごと上書きする。
#   つまり ①案件取得が壊れる → ②復元できず申請が消える → ③利用者が1件でも編集する
#   → ④欠けた状態でSupabaseが上書きされ **お客様の入力が永久に失われる**。
#   実際に2026-08-09〜17に①②が起き、③の寸前だった（上書き前に復旧できたのは幸運）。
#
# 対策: 解決できなかった申請をここに保持し、Supabaseへ書き戻すときに必ず混ぜる。
# 案件データが直れば次の起動で正しく復元される。案件が一時的に見つからないことは
# 「お客様の入力を消してよい理由」には決してならない。
_unlinked_apps: list[dict[str, Any]] = []

# 仕様書の取得可否ステータス（取れる/取れない/不明）
SPEC_AVAILABLE = "available"      # ダウンロード可能
SPEC_UNAVAILABLE = "unavailable"  # 取得不可（理由は spec_reason）
SPEC_UNKNOWN = "unknown"          # 未判定

# 仕様書が「取れない」理由の分類（取れないなら なんで取れないか）
SPEC_REASONS: dict[str, str] = {
    "login_required": "電子入札システムへのログイン／事業者登録が必要",
    "in_person": "窓口・現地での図書受領のみ（郵送/DL不可）",
    "paid": "有料（実費負担・閲覧のみ）",
    "request_form": "交付申請書の提出後に交付",
    "period_closed": "公開期間が終了している",
    "not_published": "仕様書がWeb未公開（発注機関へ要問合せ）",
}


SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT    NOT NULL DEFAULT 'PPI',   -- 取得元（PPI など）
    external_id   TEXT    UNIQUE,                    -- 取得元での一意ID
    title         TEXT    NOT NULL,                  -- 案件名
    agency        TEXT    DEFAULT '',                -- 発注機関
    agency_type   TEXT    DEFAULT '',                -- 都道府県/市区町村/国/独法 等
    region        TEXT    DEFAULT '',                -- 地方区分（北海道・東北 等）
    prefecture    TEXT    DEFAULT '',                -- 都道府県
    category      TEXT    DEFAULT '',                -- 業種（電気工事 等）
    procurement_type TEXT DEFAULT '',                -- 調達区分（工事/役務/物品）
    bid_method    TEXT    DEFAULT '',                -- 入札方式（一般競争入札 等）
    announced_date TEXT   DEFAULT '',                -- 公告日（ISO）
    deadline      TEXT    DEFAULT '',                -- 申込締切（ISO）
    detail_url    TEXT    DEFAULT '',                -- 案件詳細URL
    spec_status   TEXT    DEFAULT 'unknown',         -- 仕様書 取得可否
    spec_reason   TEXT    DEFAULT '',                -- 取れない理由コード（SPEC_REASONS）
    spec_url      TEXT    DEFAULT '',                -- 仕様書URL（取得可のとき）
    budget        TEXT    DEFAULT '',                -- 予定価格等（表示用テキスト・任意）
    budget_yen    INTEGER DEFAULT 0,                 -- 予定価格（円・数値。金額フィルタ/整列用）
    winner        TEXT    DEFAULT '',                -- 落札者（競合企業分析の核）
    win_price     TEXT    DEFAULT '',                -- 落札価格
    description   TEXT    DEFAULT '',                -- 案件説明（締切抽出元の自由記述）
    sector        TEXT    DEFAULT '公共',            -- 区分（公共/民間）。手動追加で民間も管理する
    created_at    TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cases_pref     ON cases(prefecture);
CREATE INDEX IF NOT EXISTS idx_cases_region   ON cases(region);
CREATE INDEX IF NOT EXISTS idx_cases_category ON cases(category);
CREATE INDEX IF NOT EXISTS idx_cases_deadline ON cases(deadline);
-- procurement_type / budget_yen の索引は init_db() のマイグレーション後に作成
-- （既存DBでは列追加が先に必要なため）。

CREATE TABLE IF NOT EXISTS profile (
    id            INTEGER PRIMARY KEY CHECK (id = 1),  -- 単一行
    company       TEXT DEFAULT '',   -- 自社名（競合一覧から自社を除外する）
    prefectures   TEXT DEFAULT '',   -- 対応エリア（都道府県, カンマ区切り）
    categories    TEXT DEFAULT '電気工事',  -- 対応業種（カンマ区切り）
    budget_max    TEXT DEFAULT '',   -- 予算上限（予定価格がこれ以下）。空=制限なし
    grade         TEXT DEFAULT '',   -- 経審等級（A〜E, 参考）
    quals         TEXT DEFAULT '',   -- 保有資格メモ
    representative TEXT DEFAULT '',  -- 代表者氏名
    address       TEXT DEFAULT '',   -- 本社所在地
    corp_number   TEXT DEFAULT '',   -- 法人番号
    qualifications TEXT DEFAULT '[]',-- 入札参加資格・等級（機関別, JSON配列）
    updated_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS applications (
    case_id        INTEGER PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
    status         TEXT NOT NULL DEFAULT '参加申請準備前',  -- APP_STATUSES のいずれか
    applied_date   TEXT DEFAULT '',                 -- 申請日（任意）
    note           TEXT DEFAULT '',                 -- メモ
    assignee       TEXT DEFAULT '',                 -- 担当者（社長／金子さん 等）
    apply_deadline TEXT DEFAULT '',                 -- 参加申請期限（ISO・空なら案件の締切を流用）
    bid_deadline   TEXT DEFAULT '',                 -- 入札書提出期限（ISO）
    open_date      TEXT DEFAULT '',                 -- 開札日（ISO）
    submit_method  TEXT DEFAULT '',                 -- 入札タイプ（電子システム／郵送 等）
    work           TEXT DEFAULT '',                 -- 工事カテゴリ（空なら案件のcategory）
    materials      TEXT DEFAULT '',                 -- 資料受取メモ（例: 6/1以降受取）
    flag           TEXT DEFAULT '',                 -- 要確認フラグの一言（例: 資料待ち）
    needs_check    INTEGER DEFAULT 0,               -- 要確認フラグ（0/1）
    bid_plan       INTEGER DEFAULT 0,               -- 入札予定額（円）
    win_amount     INTEGER DEFAULT 0,               -- 落札額（円）
    award_called   INTEGER DEFAULT 0,               -- 落札連絡済み（0/1）
    partner        TEXT DEFAULT '',                 -- 発注先の協力会社（採用見積の会社名）
    partners       TEXT DEFAULT '[]',               -- 協力会社見積（quotes・JSON配列）
    agency_override TEXT DEFAULT '',                -- 元機関(発注機関)の上書き（案件のagency修正用）
    updated_at     TEXT DEFAULT (datetime('now'))
);

-- AI応募アシストの生成結果キャッシュ（external_id=再採番に強い安定キー）。
-- 同じ案件の再タップで再課金しないために保持する（無料プランでは一切書かれない）。
CREATE TABLE IF NOT EXISTS ai_assist (
    external_id TEXT PRIMARY KEY,     -- 取得元の一意ID
    payload     TEXT NOT NULL,        -- 生成結果(JSON)
    model       TEXT DEFAULT '',      -- 使用モデル
    created_at  TEXT DEFAULT (datetime('now'))
);

-- NG理由集計の保存記録（月次レポートのスナップショット）。
-- ai_assist のキャッシュと違い「その時点の記録」として残す。Supabase KVにも write-through。
CREATE TABLE IF NOT EXISTS ng_reports (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT (datetime('now', '+9 hours')),  -- JST表示用
    sheet      TEXT DEFAULT '公共',
    payload    TEXT NOT NULL          -- 集計結果(JSON)
);

-- AI使用量の集計（月×機能×モデル）。従量請求の根拠を明瞭化するために保持する。
-- 生イベントではなく月次集計で持つ（サイズが伸びない）。Supabase KVにも write-through。
CREATE TABLE IF NOT EXISTS ai_usage (
    month         TEXT NOT NULL,            -- 'YYYY-MM'（JST）
    kind          TEXT NOT NULL,            -- 機能名（応募アシスト/案件AI概要/NG理由集計 等）
    model         TEXT NOT NULL DEFAULT '', -- 使用モデル（押した回数の行は ''）
    calls         INTEGER DEFAULT 0,        -- API呼び出し回数（キャッシュ応答は数えない）
    prompt_tokens INTEGER DEFAULT 0,        -- 入力トークン累計
    output_tokens INTEGER DEFAULT 0,        -- 出力トークン累計（思考トークン含む）
    taps          INTEGER DEFAULT 0,        -- ボタンを押した回数（キャッシュ応答も含む）
    PRIMARY KEY (month, kind, model)
);

CREATE TABLE IF NOT EXISTS agencies (
    name        TEXT PRIMARY KEY,    -- 発注機関名
    njss_count  INTEGER DEFAULT 0,   -- NJSS案件数（規模の目安）
    top_url     TEXT DEFAULT '',     -- 公式トップURL
    domain      TEXT DEFAULT '',     -- ドメイン（使用プラットフォームの判別に）
    platform_n  INTEGER DEFAULT 0,   -- 共通基盤_機関数
    bid_url     TEXT DEFAULT '',     -- 公式入札情報ページ
    sample_url  TEXT DEFAULT '',     -- NJSS案件URL例
    fetched_at  TEXT DEFAULT ''      -- 取得日時
);

-- 監視機関のホワイトリスト除外（チェックを外した発注機関＝案件一覧に出さない）。
-- 既定は「全機関ON（除外なし）」。ここに入っている機関名の案件だけ非表示にする。
CREATE TABLE IF NOT EXISTS agency_exclusions (
    name TEXT PRIMARY KEY
);

-- 協力会社マスタ（bid-next-eta の X 配列に相当）。
CREATE TABLE IF NOT EXISTS companies (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL,            -- 会社名
    area    TEXT DEFAULT '',          -- 対応エリア
    tags    TEXT DEFAULT '[]',        -- 工事カテゴリ（JSON配列）
    tel     TEXT DEFAULT '',          -- 電話番号
    url     TEXT DEFAULT '',          -- 会社URL
    note    TEXT DEFAULT '',          -- メモ／特徴
    partner INTEGER DEFAULT 0,        -- ★よく頼む（0/1）
    rating  INTEGER DEFAULT 0,        -- 評価（0〜5）
    reviews TEXT DEFAULT '[]'         -- 口コミ（JSON配列）
);
"""

# 入札参加申請の進行ステータス（bid-next-eta＝川野電気システムのカンバン列と完全一致）。
APP_STATUSES: list[str] = [
    "参加申請準備前",   # これから参加申請する
    "入札参加申請済み", # 参加申請を提出済み
    "協力会社探し中",   # 見積を出してくれる協力会社を探している
    "見積取得",         # 協力会社の見積を回収中／取得済み
    "入札書提出済み",   # 入札書を提出した（開札待ち）
    "自社落札",         # 自社が落札
    "他社落札",         # 他社が落札（失注）
    "NG",               # 不参加／見送り
    "見積集まらず",     # 見積が集まらず対応困難
]

# カンバン列のアクセント色（bid-next-eta の B マップと一致）。
STATUS_ACCENT: dict[str, str] = {
    "参加申請準備前": "#9aa3ad",
    "入札参加申請済み": "#2563eb",
    "協力会社探し中": "#0891b2",
    "見積取得": "#7c3aed",
    "入札書提出済み": "#db2777",
    "自社落札": "#16a34a",
    "他社落札": "#64748b",
    "NG": "#dc2626",
    "見積集まらず": "#b45309",
    # 民間シート専用の状況（下記 APP_STATUSES_PRIVATE）の色。
    "見積中": "#0891b2",
    "見積完了": "#7c3aed",
    "提出確認中": "#db2777",
    "提出完了": "#2563eb",
    "失注か受注か保留中": "#64748b",
}

# 民間シート専用の状況（公共の入札フローとは別の、見積〜提出〜結果の流れ）。
# 公共(APP_STATUSES)はそのまま。民間タブだけこの列でカンバンを描く。
APP_STATUSES_PRIVATE: list[str] = [
    "見積中",
    "見積完了",
    "提出確認中",
    "提出完了",
    "失注か受注か保留中",
]

# 保存時の検証に使う全状況（公共・民間どちらの状況も受け付ける）。
APP_STATUSES_ALL: list[str] = APP_STATUSES + APP_STATUSES_PRIVATE

# 旧ステータス → 現ステータスの読み替え（既存データ・localStorage退避分の救済）。
STATUS_ALIASES: dict[str, str] = {
    # 直前リリースの暫定名
    "検討中": "参加申請準備前",
    "参加申請準備中": "参加申請準備前",
    "参加申請済": "入札参加申請済み",
    "入札書提出済": "入札書提出済み",
    "落札": "自社落札",
    "見送り": "NG",
    # さらに旧い名前
    "申請準備中": "参加申請準備前",
    "申請済": "入札参加申請済み",
    "入札参加済": "入札書提出済み",
    "不参加": "NG",
}

# 担当者（bid-next-eta の G/$）。色も一致。
ASSIGNEES: list[str] = ["社長", "金子さん", "上西さん", "未割当"]
ASSIGNEE_COLOR: dict[str, str] = {
    "社長": "#16a34a", "金子さん": "#2563eb",
    "上西さん": "#ea580c", "未割当": "#a8a29e",
}

# 工事カテゴリの色（bid-next-eta の V マップと一致）。
WORK_COLOR: dict[str, str] = {
    "電気工事": "#2563eb", "空調": "#0891b2", "照明/LED": "#d97706",
    "防犯/カメラ": "#dc2626", "通信/弱電": "#7c3aed", "管工事": "#0d9488",
    "太陽光": "#ca8a04", "高圧受電": "#4338ca", "リフォーム/建築": "#b45309",
    "制御盤": "#475569", "足場": "#65a30d", "清掃": "#16a34a",
    "IT/システム": "#0ea5e9", "商社/卸": "#9333ea", "土木": "#78716c",
}

# 入札タイプ（提出方法）。
SUBMIT_METHODS: list[str] = ["電子システム", "電子", "郵送", "持参", "郵送/持参"]


def normalize_status(status: str) -> str:
    """旧ステータス名を現行名に読み替える。未知の値はそのまま返す。"""
    s = (status or "").strip()
    return STATUS_ALIASES.get(s, s)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """スキーマを作成（既存なら何もしない）＋軽量マイグレーション。"""
    with _connect() as conn:
        conn.executescript(SCHEMA)
        # 既存DBに後から追加した列をマイグレーション（無ければ追加）
        cols = [r[1] for r in conn.execute("PRAGMA table_info(profile)")]
        if "company" not in cols:
            conn.execute("ALTER TABLE profile ADD COLUMN company TEXT DEFAULT ''")
        for col, ddl in (
            ("representative", "TEXT DEFAULT ''"),
            ("address",        "TEXT DEFAULT ''"),
            ("corp_number",    "TEXT DEFAULT ''"),
            ("qualifications", "TEXT DEFAULT '[]'"),
        ):
            if col not in cols:
                conn.execute(f"ALTER TABLE profile ADD COLUMN {col} {ddl}")
        # cases の後付け列をマイグレーション（無ければ追加）
        case_cols = [r[1] for r in conn.execute("PRAGMA table_info(cases)")]
        if "description" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN description TEXT DEFAULT ''")
        if "procurement_type" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN procurement_type TEXT DEFAULT ''")
        if "budget_yen" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN budget_yen INTEGER DEFAULT 0")
        if "sector" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN sector TEXT DEFAULT '公共'")
        # 列追加後に索引を作成（新設列のため SCHEMA からは外してある）
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_proctype ON cases(procurement_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_budgetyen ON cases(budget_yen)")
        # applications の後付け列をマイグレーション（無ければ追加）。
        # bid-next-eta（川野電気システム）の管理機能を移植するために拡張した列。
        app_cols = [r[1] for r in conn.execute("PRAGMA table_info(applications)")]
        for col, ddl in (
            ("assignee",       "TEXT DEFAULT ''"),
            ("apply_deadline", "TEXT DEFAULT ''"),
            ("bid_deadline",   "TEXT DEFAULT ''"),
            ("open_date",      "TEXT DEFAULT ''"),
            ("submit_method",  "TEXT DEFAULT ''"),
            ("work",           "TEXT DEFAULT ''"),
            ("materials",      "TEXT DEFAULT ''"),
            ("flag",           "TEXT DEFAULT ''"),
            ("needs_check",    "INTEGER DEFAULT 0"),
            ("bid_plan",       "INTEGER DEFAULT 0"),
            ("win_amount",     "INTEGER DEFAULT 0"),
            ("award_called",   "INTEGER DEFAULT 0"),
            ("partner",        "TEXT DEFAULT ''"),
            ("partners",       "TEXT DEFAULT '[]'"),
            ("agency_override", "TEXT DEFAULT ''"),
            ("spec_files",     "TEXT DEFAULT '[]'"),  # 仕様書の紐付け（URL/添付ファイル・要望⑦STEP1）
            ("win_company",    "TEXT DEFAULT ''"),    # 落札会社名（自社・他社問わず最終落札先）
            ("cost_items",     "TEXT DEFAULT '[]'"),  # 自社原価の内訳（[{label,amount}]・JSON）
            ("client_mtime",   "INTEGER DEFAULT 0"),  # 端末側の編集時刻(ms)。restoreの新旧判定に使う
            ("inquiry_period", "TEXT DEFAULT ''"),    # 質疑（質問書）受付期間（要望⑱-①）
        ):
            if col not in app_cols:
                conn.execute(f"ALTER TABLE applications ADD COLUMN {col} {ddl}")
        # companies: 公共/民間の切り分け（既存の登録は全て公共案件由来）
        co_cols = [r[1] for r in conn.execute("PRAGMA table_info(companies)")]
        if "sector" not in co_cols:
            conn.execute("ALTER TABLE companies ADD COLUMN sector TEXT DEFAULT '公共'")
        # ai_usage: 押した回数（キャッシュ応答含む）の後付け列
        au_cols = [r[1] for r in conn.execute("PRAGMA table_info(ai_usage)")]
        if au_cols and "taps" not in au_cols:
            conn.execute("ALTER TABLE ai_usage ADD COLUMN taps INTEGER DEFAULT 0")
        # 仕様書ファイルの実体（BLOB）。案件JSONを軽く保つため別テーブルに保管。
        conn.execute(
            "CREATE TABLE IF NOT EXISTS spec_blobs ("
            " key TEXT PRIMARY KEY, mime TEXT DEFAULT '', name TEXT DEFAULT '',"
            " data BLOB, created_at TEXT DEFAULT (datetime('now')))"
        )
        conn.commit()
    # Supabaseの保存内容をSQLiteへ復元（揮発DB対策）。未設定/不通でも黙って続行。
    try:
        supa.init()
        restore_from_supa()
    except Exception:  # noqa: BLE001 — 永続化層の失敗でアプリ起動を妨げない
        pass


def upsert_cases(rows: list[dict[str, Any]]) -> int:
    """案件を一括投入。external_id が衝突したら上書き更新。投入件数を返す。"""
    cols = [
        "source", "external_id", "title", "agency", "agency_type",
        "region", "prefecture", "category", "procurement_type", "bid_method",
        "announced_date", "deadline", "detail_url", "spec_status", "spec_reason",
        "spec_url", "budget", "budget_yen", "winner", "win_price", "description",
        "sector",
    ]
    placeholders = ", ".join(["?"] * len(cols))
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "external_id")
    sql = (
        f"INSERT INTO cases ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(external_id) DO UPDATE SET {updates}"
    )

    def _val(r: dict[str, Any], c: str) -> Any:
        # budget_yen は数値列。未設定や非数値は 0 に正規化する。
        if c == "budget_yen":
            v = r.get(c, 0)
            try:
                return int(v) if v not in ("", None) else 0
            except (TypeError, ValueError):
                return 0
        # sector 未指定の取得元（スクレイパ）は公共とみなす。
        if c == "sector":
            return r.get(c) or "公共"
        return r.get(c, "")

    with _connect() as conn:
        conn.executemany(sql, [tuple(_val(r, c) for c in cols) for r in rows])
        conn.commit()
    return len(rows)


# 添付サイズ表記など、案件名に混入する飾りの除去（突合キー用）。
# 揺れの実例: (108kbyte) (PDF:28KB) (PDF98KB) (PDF 242 KB) (28.3KB)
_TITLE_NOISE_RE = re.compile(
    r"[（(]\s*(?:pdf[:：]?\s*)?[\d.,]+\s*(?:kb|kbyte|mb|kバイト|キロバイト)\s*[）)]",
    re.IGNORECASE)
# 再公告マーカー（同一案件のやり直し公告。突合時は締切が変わっていても同一とみなす）
# 書き方の揺れ: （再公告）（再度公告）（再再度公告）（再再再度公告）（再々公告）
# （再入札）（再度）、および【再度公告】のような【】書き
_REPOST_RE = re.compile(r"[（(【](再入札|再+々?度*公告|再度)[）)】]")
# 先頭の飾り【…】。地区名などを消さないよう、状態・案内系の語を含むものだけ除去する
# （例:【終了しました】【入札公告】【お知らせ】。【A地区】等は別案件の識別子なので残す）
_LEAD_BRACKET_RE = re.compile(r"^【[^】]*(終了|公告|入札|お知らせ|案内|募集|受付)[^】]*】")


def _norm_key(text: str) -> str:
    """重複突合用の正規化キー（全半角・空白・括弧・添付サイズ表記の揺れを吸収）。"""
    import unicodedata
    t = unicodedata.normalize("NFKC", str(text or "")).lower()
    t = _TITLE_NOISE_RE.sub("", t)
    return re.sub(r"\s+", "", t)


def _norm_title_key(title: str) -> str:
    """案件名の突合キー。_norm_key に加え、状態プレフィックスと再公告マーカーを除く。

    自治体は閉札後にページ名を「【終了しました】…」へ書き換え、官公需APIがそれを
    別記事として再収集する（同じ案件が2〜3行になる主因）。再公告(（再度公告）等)も
    同じ案件のやり直しなので同名として束ねる。
    """
    import unicodedata
    t = unicodedata.normalize("NFKC", str(title or ""))
    t = _LEAD_BRACKET_RE.sub("", t)
    t = _REPOST_RE.sub("", t)
    return _norm_key(t)


def _parse_iso(d: str):
    """ISO日付文字列 → date（不正・空は None）。"""
    from datetime import date as _date
    try:
        return _date.fromisoformat((d or "")[:10])
    except ValueError:
        return None


def dedupe_cases(window_days: int = 150) -> int:
    """取得元をまたいだ同一案件の重複を統合し、削除した件数を返す。

    実データを読んで確認した重複の発生原因と対応（2026-07-27 監査）:
      1. 自治体が閉札後にページ名を「【終了しました】…」へ書き換え、官公需APIが
         別記事として再収集 → _norm_title_key で状態プレフィックスを除いて突合
      2. 再公告（（再度公告）等）は締切が変わる → マーカー付きの行は締切が
         食い違っても同一クラスタに束ねる（新しい公告が古い公告を引き継ぐ）
      3. 同じページを官公需APIが後日再インデックス（公告日だけ更新される）
         → 同一の個別detail_url なら日付に関係なく同一とみなす
      4. 同じ公告が「省庁」と「地方支分部局」の両方の機関名で二重登録される
         （例: 法務省／法務省札幌法務局）→ 機関名が包含関係かつ締切一致等の
         強い一致がある場合のみ同一とみなす
      5. 調達ポータル落札実績は落札発表日が公告から数ヶ月遅れる
         → 落札実績が絡む突合だけ日付窓を広げる（落札者の後付けを成立させる）

    誤統合を避けるため据え置く（統合しない）と確認したもの:
      - 同名で締切が異なるシリーズ工事（例: 福島県「道路橋りょう維持工事」。
        工期・公告PDFが異なる別契約が同名で多数発注される）
      - 公告日が年度をまたぐ同名案件（毎年の再調達）
    """
    cols = ("id", "external_id", "source", "title", "agency", "deadline",
            "announced_date", "detail_url", "spec_status", "spec_reason", "spec_url",
            "budget", "budget_yen", "winner", "win_price", "description")
    with _connect() as conn:
        rows = [dict(zip(cols, r)) for r in conn.execute(
            f"SELECT {', '.join(cols)} FROM cases WHERE source != 'manual'")]
        app_ids = {r[0] for r in conn.execute("SELECT case_id FROM applications")}

    import procurement  # 汎用URL判定（procurementはdb非依存＝循環しない）

    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = _norm_title_key(r["title"])
        if not key:
            continue
        r["_na"] = _norm_key(r["agency"])                       # 機関名の突合キー
        r["_marker"] = bool(_REPOST_RE.search(r["title"] or "")) # 再公告マーカー
        r["_award"] = r["source"] == "調達ポータル落札実績"
        u = (r["detail_url"] or "").strip()
        r["_url"] = u if procurement.is_real_link(u) else ""     # 個別ページのURLのみ
        groups.setdefault(key, []).append(r)

    # 引き継ぎ対象（残す行の値が空/0のときだけ消す行から補完する）
    fill_cols = ("deadline", "announced_date", "detail_url", "spec_url",
                 "budget", "winner", "win_price", "description")
    to_delete: list[int] = []
    keeper_updates: dict[int, dict[str, Any]] = {}

    def _agency_compat(na: str, cl: dict) -> tuple[bool, bool]:
        """(互換か, 包含関係による互換か)。同一機関 or 片方が他方を含む機関名なら互換。"""
        for a in cl["agencies"]:
            if na == a:
                return True, False
            if na and a and (na in a or a in na):
                return True, True
        return False, False

    for key, group in groups.items():
        if len(group) < 2:
            continue
        # 公告日順に並べ、同一案件と判断できる行を同一クラスタに束ねる
        group.sort(key=lambda r: (r["announced_date"] or "9999-12-31", r["id"]))
        clusters: list[dict] = []
        for r in group:
            d, ad = (r["deadline"] or "").strip()[:10], _parse_iso(r["announced_date"])
            placed = False
            for cl in clusters:
                # 原因3: 同一の個別URLは日付・機関表記に関係なく同一案件
                if r["_url"] and r["_url"] in cl["urls"]:
                    placed = True
                else:
                    ag_ok, ag_contain = _agency_compat(r["_na"], cl)
                    if not ag_ok:
                        continue
                    # 原因4: 省庁と支分部局が同じ公告を同日に二重登録し、ページ差で
                    # 締切の読み取りだけズレるパターン。案件名が十分固有（正規化20字
                    # 以上）なら同一とみなす。短い汎用名は別組織の同名調達があり得る
                    # ので対象外
                    same_day = bool(r["announced_date"]) and r["announced_date"] in cl["ann_dates"]
                    dual_reg = same_day and ag_contain and len(key) >= 20
                    # 原因2: 再公告（または上記の二重登録）の場合のみ締切の食い違いを許す
                    if d and cl["deadline"] and d != cl["deadline"] \
                            and not (r["_marker"] or cl["has_marker"] or dual_reg):
                        continue
                    # 原因5: 落札実績が絡む場合は日付窓を広げる（発表が数ヶ月遅れる）
                    win = 270 if (r["_award"] or cl["has_award"]) else window_days
                    if ad and cl["last_date"] and (ad - cl["last_date"]).days > win:
                        continue
                    # 機関名が包含関係どまり（完全一致でない）のときは強い一致＝
                    # 締切一致／落札実績／同日二重登録／同日の再公告 が無い限り
                    # 別案件とみなす（別組織の同名調達を守る）
                    if ag_contain and not (
                            (d and cl["deadline"] and d == cl["deadline"])
                            or (r["_award"] or cl["has_award"]) or dual_reg
                            or (same_day and (r["_marker"] or cl["has_marker"]))):
                        continue
                    placed = True
                if placed:
                    cl["rows"].append(r)
                    cl["deadline"] = cl["deadline"] or d
                    if ad:
                        cl["last_date"] = max(cl["last_date"] or ad, ad)
                    cl["agencies"].add(r["_na"])
                    if r["announced_date"]:
                        cl["ann_dates"].add(r["announced_date"])
                    if r["_url"]:
                        cl["urls"].add(r["_url"])
                    cl["has_marker"] = cl["has_marker"] or r["_marker"]
                    cl["has_award"] = cl["has_award"] or r["_award"]
                    break
            if not placed:
                clusters.append({"rows": [r], "deadline": d, "last_date": ad,
                                 "agencies": {r["_na"]}, "urls": {r["_url"]} - {""},
                                 "ann_dates": {r["announced_date"]} - {""},
                                 "has_marker": r["_marker"], "has_award": r["_award"]})

        for cl in clusters:
            crows = cl["rows"]
            if len(crows) < 2:
                continue
            # 残す1件: 申請あり＞詳細URLあり＞締切あり＞「終了」表記でない＞
            # 公告日が新しい＞再公告＞id大
            # （「【終了しました】…」の書き換え版ではなく元の綺麗な題名を残す。
            #   同日なら再公告側＝最新のやり直し公告を残す）
            keeper = max(crows, key=lambda r: (
                r["id"] in app_ids, bool(r["detail_url"]), bool(r["deadline"]),
                "終了" not in (r["title"] or ""),
                r["announced_date"] or "", r["_marker"], r["id"]))
            fills = keeper_updates.setdefault(keeper["id"], {})
            for r in crows:
                if r["id"] == keeper["id"]:
                    continue
                if r["id"] in app_ids:
                    continue   # 申請つきは消さない（残す行と並存させる）
                for c in fill_cols:
                    if not (keeper.get(c) or fills.get(c)) and r.get(c):
                        fills[c] = r[c]
                        # 仕様書URLを引き継ぐ時は取得可否・理由もセットで
                        if c == "spec_url":
                            fills["spec_status"] = r["spec_status"]
                            fills["spec_reason"] = r["spec_reason"]
                if not keeper["budget_yen"] and r["budget_yen"]:
                    fills["budget_yen"] = r["budget_yen"]
                to_delete.append(r["id"])
            # 原因4（省庁/支分部局の二重登録）を束ねた場合は、より具体的な機関名を残す
            best_ag = max((r["agency"] or "" for r in crows), key=len)
            if best_ag and keeper["agency"] and best_ag != keeper["agency"] \
                    and _norm_key(keeper["agency"]) in _norm_key(best_ag):
                fills["agency"] = best_ag
            if not fills:
                keeper_updates.pop(keeper["id"], None)

    if not to_delete:
        return 0
    with _connect() as conn:
        for cid, fills in keeper_updates.items():
            sets = ", ".join(f"{c} = ?" for c in fills)
            conn.execute(f"UPDATE cases SET {sets} WHERE id = ?",
                         (*fills.values(), cid))
        conn.executemany("DELETE FROM cases WHERE id = ?",
                         [(i,) for i in to_delete])
        conn.commit()
    return len(to_delete)


def add_manual_case(title: str, agency: str = "", sector: str = "公共",
                    category: str = "") -> tuple[int, str]:
    """手動で案件を1件作成する（民間・公共どちらも）。(case_id, external_id) を返す。

    external_id は再採番・揮発DBに強い安定キー。手動案件は復元時に案件本体を
    再生成できるよう 'manual:' 接頭辞で識別する。
    """
    import uuid
    ext = "manual:" + uuid.uuid4().hex
    upsert_cases([{
        "source": "manual", "external_id": ext,
        "title": (title or "").strip() or "（無題の案件）",
        "agency": (agency or "").strip(), "category": (category or "").strip(),
        "sector": sector if sector in ("公共", "公共役務", "民間") else "公共",
    }])
    cid = get_case_id_by_external(ext)
    return (cid, ext)


def update_case_title(case_id: int, title: str) -> bool:
    """手動案件(source='manual')の案件名を変更する。

    スクレイプ案件は毎日の更新で元の名称に戻ってしまうため対象外。
    変更があればSupabaseスナップショット（_case_title）も更新する。
    """
    title = (title or "").strip()
    if not title:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE cases SET title = ? WHERE id = ? AND source = 'manual' AND title != ?",
            (title, case_id, title),
        )
        conn.commit()
        changed = cur.rowcount > 0
    if changed:
        _push_applications()
    return changed


def _as_list(v: Any) -> list[str]:
    """str / list / None を、空要素を除いた文字列リストに正規化する。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [str(x).strip() for x in v if str(x).strip()]


def _build_case_filter(
    *,
    region: str | None = None,
    prefecture: str | None = None,
    category: str | list[str] | None = None,
    procurement_type: str | list[str] | None = None,
    bid_method: str | list[str] | None = None,
    spec_status: str | None = None,
    budget_min: int | None = None,
    open_only: bool = False,
    hide_closed: bool = False,
    q: str = "",
    agency: str = "",
    announced_after: str | None = None,
    exclude_agencies: list[str] | set[str] | None = None,
) -> tuple[str, list[Any]]:
    """絞り込み条件から WHERE句とパラメータを組み立てる（list_cases と件数で共用）。"""
    where: list[str] = []
    params: list[Any] = []

    # 監視機関のチェックを外した発注機関は案件一覧から除外する。
    exc = [a for a in (exclude_agencies or []) if a]
    if exc:
        where.append("agency NOT IN (%s)" % ",".join("?" * len(exc)))
        params.extend(exc)

    if prefecture:
        where.append("prefecture = ?")
        params.append(prefecture)
    elif region:
        where.append("region = ?")
        params.append(region)

    # 業種・区分・入札方式は複数選択（OR）に対応
    for col, val in (("category", category), ("procurement_type", procurement_type),
                     ("bid_method", bid_method)):
        vals = _as_list(val)
        if vals:
            where.append(f"{col} IN (%s)" % ",".join("?" * len(vals)))
            params.extend(vals)

    if spec_status:
        where.append("spec_status = ?")
        params.append(spec_status)
    if budget_min:
        where.append("budget_yen >= ?")
        params.append(int(budget_min))
    if open_only:
        # 締切が判明していて、かつ今日以降のものだけ＝実際に応募できる案件
        where.append("deadline != '' AND deadline >= date('now', 'localtime')")
    if hide_closed:
        # 締切が過去のもの＝終了を隠す。締切不明('')や今後分は残す。
        where.append("(deadline = '' OR deadline >= date('now', 'localtime'))")
    if announced_after:
        # 新着フィルタ（公告日がこの日以降）。SQL側で行うことで件数も正確・上限の影響を受けない。
        where.append("announced_date != '' AND announced_date >= ?")
        params.append(announced_after)
    # 発注機関でしぼる（要望⑧）。機関名の部分一致。
    if agency and agency.strip():
        where.append("agency LIKE ?")
        params.append(f"%{agency.strip()}%")
    # キーワードは空白/カンマ区切りで複数可。各語が title/agency いずれかに一致（語間OR）
    terms = [t for t in q.replace("，", ",").replace("、", ",").replace(",", " ").split() if t]
    if terms:
        ors = " OR ".join("(title LIKE ? OR agency LIKE ?)" for _ in terms)
        where.append(f"({ors})")
        for t in terms:
            params.extend([f"%{t}%", f"%{t}%"])

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    return clause, params


def count_list_cases(**filters: Any) -> int:
    """list_cases と同じ絞り込み条件に該当する件数（上限なしの実数）。"""
    # sort/limit/offset は件数に無関係なので除外
    for k in ("sort", "limit", "offset"):
        filters.pop(k, None)
    clause, params = _build_case_filter(**filters)
    with _connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM cases {clause}", params).fetchone()[0]


def list_cases(
    *,
    region: str | None = None,
    prefecture: str | None = None,
    category: str | list[str] | None = None,
    procurement_type: str | list[str] | None = None,
    bid_method: str | list[str] | None = None,
    spec_status: str | None = None,
    budget_min: int | None = None,
    open_only: bool = False,
    hide_closed: bool = False,
    q: str = "",
    agency: str = "",
    announced_after: str | None = None,
    exclude_agencies: list[str] | set[str] | None = None,
    sort: str = "deadline",
    limit: int = 200,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """条件で案件を絞り込む（NJSS風の段階フィルタ）。

    都道府県が指定されればそれを優先、無ければ地方区分で絞る。
      - category / procurement_type / bid_method: str でも list でも可（list は OR）
      - budget_min: 予定価格(円)がこれ以上（1000万＝10000000 等）
      - open_only: 締切が今日以降の「今応募できる」案件のみ
      - hide_closed: 締切が過去の「終了」案件を隠す（締切不明・今後分は残す）
      - announced_after: 公告日がこの日以降（新着フィルタ）
      - q: 空白/カンマ区切りで複数キーワード可（いずれか一致＝OR）
    """
    clause, params = _build_case_filter(
        region=region, prefecture=prefecture, category=category,
        procurement_type=procurement_type, bid_method=bid_method,
        spec_status=spec_status, budget_min=budget_min, open_only=open_only,
        hide_closed=hide_closed, q=q, agency=agency, announced_after=announced_after,
        exclude_agencies=exclude_agencies,
    )
    # 締切が空文字の案件は末尾に回す
    order = {
        "deadline": "CASE WHEN deadline = '' THEN 1 ELSE 0 END, deadline ASC",
        "announced": "announced_date DESC",
        "budget": "budget_yen DESC",
    }.get(sort, "deadline ASC")

    sql = f"SELECT * FROM cases {clause} ORDER BY {order} LIMIT ? OFFSET ?"
    params = list(params) + [limit, offset]
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_case(case_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return dict(row) if row else None


def get_ai_assist(external_id: str) -> dict[str, Any] | None:
    """AI応募アシストのキャッシュ結果を返す（無ければ None）。payload はJSON文字列のまま。"""
    if not external_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload, model, created_at FROM ai_assist WHERE external_id = ?",
            (external_id,)).fetchone()
        return dict(row) if row else None


def get_ai_assist_sum_latest(external_id: str) -> dict[str, Any] | None:
    """その案件の最新のAI概要キャッシュを返す（仕様書件数が変わり完全一致キーが無いとき用）。"""
    if not external_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT payload, model, created_at FROM ai_assist "
            "WHERE external_id LIKE 'sum:%' AND external_id LIKE ? "
            "ORDER BY created_at DESC LIMIT 1",
            ("%:" + external_id,)).fetchone()
        return dict(row) if row else None


def set_ai_assist(external_id: str, payload: str, model: str = "") -> None:
    """AI応募アシストの結果をキャッシュに保存（external_id で上書き）。"""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO ai_assist (external_id, payload, model, created_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(external_id) DO UPDATE SET
                 payload=excluded.payload, model=excluded.model,
                 created_at=excluded.created_at""",
            (external_id, payload, model))
        conn.commit()


def list_ng_reports(sheet: str = "") -> list[dict[str, Any]]:
    """保存したNG集計の記録（新しい順）。payload はJSON文字列のまま返す。"""
    with _connect() as conn:
        if sheet:
            rows = conn.execute(
                "SELECT * FROM ng_reports WHERE sheet = ? ORDER BY id DESC",
                (sheet,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ng_reports ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]


def add_ng_report(sheet: str, payload: str) -> None:
    """NG集計の結果をその時点の記録として保存する。"""
    with _connect() as conn:
        conn.execute("INSERT INTO ng_reports (sheet, payload) VALUES (?, ?)",
                     (sheet or "公共", payload))
        conn.commit()
    _push_ng_reports()


def delete_ng_report(report_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM ng_reports WHERE id = ?", (report_id,))
        conn.commit()
    _push_ng_reports()


# ============================================================
# AI使用量（従量請求の根拠。月×機能×モデルで集計保持）
# ============================================================

def _jst_month() -> str:
    """現在の年月 'YYYY-MM'（JST）。"""
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m")


def add_ai_usage(kind: str, model: str, prompt_tokens: int, output_tokens: int) -> None:
    """AI呼び出し1回分の使用量を月次集計へ加算する（キャッシュ応答では呼ばない）。"""
    month = _jst_month()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO ai_usage (month, kind, model, calls, prompt_tokens, output_tokens)
               VALUES (?, ?, ?, 1, ?, ?)
               ON CONFLICT(month, kind, model) DO UPDATE SET
                 calls = calls + 1,
                 prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                 output_tokens = output_tokens + excluded.output_tokens""",
            (month, kind or "その他", model or "",
             int(prompt_tokens or 0), int(output_tokens or 0)))
        conn.commit()
    _push_ai_usage()


def add_ai_tap(kind: str) -> None:
    """AI機能のボタンを押した回数を加算する（キャッシュ応答で0円の回も含む）。

    課金対象のAPI呼び出し(calls)とは別に、利用の総量を見せるための集計。
    model='' の行に月×機能で積む。
    """
    month = _jst_month()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO ai_usage (month, kind, model, taps)
               VALUES (?, ?, '', 1)
               ON CONFLICT(month, kind, model) DO UPDATE SET taps = taps + 1""",
            (month, kind or "その他"))
        conn.commit()
    _push_ai_usage()


def list_ai_usage() -> list[dict[str, Any]]:
    """AI使用量の全集計行（新しい月→機能名順）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM ai_usage ORDER BY month DESC, kind ASC, model ASC").fetchall()
    return [dict(r) for r in rows]


def _push_ai_usage() -> None:
    # AI使用量は**請求根拠**。復元できていない状態で書き戻すと、
    # 過去の利用実績が消えてお客様に請求できなくなる。ここは特に厳格に。
    if _restoring or not _may_push("ai_usage"):
        return
    supa.save("ai_usage", list_ai_usage())


def get_case_id_by_external(external_id: str) -> int | None:
    """external_id（取得元の安定ID）から現在の案件id を引く。

    案件の整数idは日次のDB再構築で採番し直されるが external_id は不変。
    ブラウザ保存した申請をサーバへ復元する際の安定キーとして使う。
    """
    if not external_id:
        return None
    with _connect() as conn:
        row = conn.execute(
            "SELECT id FROM cases WHERE external_id = ?", (external_id,)).fetchone()
        return row[0] if row else None


def update_winner_by_external(external_id: str, winner: str,
                              win_price: str = "") -> bool:
    """既存案件（external_id 一致）の落札者・落札価格だけを更新する。

    落札結果を「別案件として重複追加」せず既存公告案件へ後付けしたい場合に使う
    最小ヘルパ。external_id が一致する案件が無ければ何もせず False を返す。
    （調達ポータル落札実績は案件番号体系が官公需APIと異なり安全に突合できないため
    現状は別レコードとして upsert している。将来突合可能になった時の更新口として用意。）
    """
    if not external_id or not winner:
        return False
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE cases SET winner = ?, win_price = ? WHERE external_id = ?",
            (winner, win_price, external_id),
        )
        conn.commit()
        return cur.rowcount > 0


def distinct_values(column: str) -> list[str]:
    """フィルタUIの候補値（指定カラムの非空ユニーク値）。"""
    allowed = {"category", "procurement_type", "bid_method", "prefecture",
               "region", "agency_type"}
    if column not in allowed:
        raise ValueError(f"許可されていないカラム: {column}")
    sql = f"SELECT DISTINCT {column} FROM cases WHERE {column} != '' ORDER BY {column}"
    with _connect() as conn:
        return [r[0] for r in conn.execute(sql).fetchall()]


def count_cases(source: str | None = None) -> int:
    """案件の総数。source 指定でその取得元のみ数える。"""
    with _connect() as conn:
        if source:
            return conn.execute(
                "SELECT COUNT(*) FROM cases WHERE source = ?", (source,)).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]


def upsert_agencies(rows: list[dict[str, Any]]) -> int:
    """監視対象の発注機関を一括投入（name で重複排除）。"""
    cols = ["name", "njss_count", "top_url", "domain", "platform_n",
            "bid_url", "sample_url", "fetched_at"]
    ph = ", ".join(["?"] * len(cols))
    upd = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "name")
    sql = (f"INSERT INTO agencies ({', '.join(cols)}) VALUES ({ph}) "
           f"ON CONFLICT(name) DO UPDATE SET {upd}")
    with _connect() as conn:
        conn.executemany(sql, [tuple(r.get(c, "") for c in cols) for r in rows])
        conn.commit()
    return len(rows)


# --- 監視機関のホワイトリスト除外（チェックを外した機関の案件を一覧から消す）---

def list_agency_exclusions() -> set[str]:
    """案件一覧から除外する発注機関名の集合。"""
    with _connect() as conn:
        return {r[0] for r in conn.execute("SELECT name FROM agency_exclusions")}


def set_agency_excluded(name: str, excluded: bool) -> None:
    """1機関の除外ON/OFF（excluded=True で除外＝チェックを外す）。"""
    name = (name or "").strip()
    if not name:
        return
    with _connect() as conn:
        if excluded:
            conn.execute("INSERT OR IGNORE INTO agency_exclusions (name) VALUES (?)", (name,))
        else:
            conn.execute("DELETE FROM agency_exclusions WHERE name = ?", (name,))
        conn.commit()
    _push_exclusions()


def set_agencies_excluded(names: list[str], excluded: bool) -> None:
    """複数機関の除外ON/OFFを一括で行う（要望③・一括ON/OFF）。

    現在の除外集合に対して、渡された機関名の部分集合だけを追加／解除する
    （replace_agency_exclusions と違い、絞り込み表示中の一部だけを操作できる）。
    """
    clean = [str(n).strip() for n in (names or []) if str(n).strip()]
    if not clean:
        return
    with _connect() as conn:
        if excluded:
            conn.executemany("INSERT OR IGNORE INTO agency_exclusions (name) VALUES (?)",
                             [(n,) for n in clean])
        else:
            conn.executemany("DELETE FROM agency_exclusions WHERE name = ?",
                             [(n,) for n in clean])
        conn.commit()
    _push_exclusions()


def replace_agency_exclusions(names: list[str]) -> None:
    """除外リストを丸ごと置き換える（localStorage からの復元用）。"""
    clean = [str(n).strip() for n in (names or []) if str(n).strip()]
    with _connect() as conn:
        conn.execute("DELETE FROM agency_exclusions")
        if clean:
            conn.executemany("INSERT OR IGNORE INTO agency_exclusions (name) VALUES (?)",
                             [(n,) for n in clean])
        conn.commit()
    _push_exclusions()


def list_agencies(q: str = "") -> list[dict[str, Any]]:
    """監視対象の発注機関一覧（案件数の多い順）。"""
    where, params = "", []
    if q:
        where = "WHERE name LIKE ? OR domain LIKE ?"
        params = [f"%{q}%", f"%{q}%"]
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM agencies {where} ORDER BY njss_count DESC", params).fetchall()]


def count_agencies() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM agencies").fetchone()[0]


@lru_cache(maxsize=4096)
def find_agency_for_case(agency_name: str) -> dict[str, Any] | None:
    """案件の発注機関名から agencies テーブルの機関情報を探す。

    官公需APIの形式 "大阪府吹田市" → agencies "吹田市役所" のようなマッチを行う。
    案件詳細を開くたびに呼ばれるため lru_cache で結果を再利用する（データは
    再デプロイ時のみ変わる＝プロセス再起動でキャッシュも自然に更新される）。

    誤マッチ防止: 短い汎用キーワード（2〜3文字）での当て推量は誤った発注機関＝
    誤った入札ポータルへの誘導につながるため、段階4は4文字以上のときだけ使う。
    """
    if not agency_name:
        return None

    with _connect() as conn:
        # 1) 完全一致
        row = conn.execute(
            "SELECT * FROM agencies WHERE name = ?", (agency_name,)
        ).fetchone()
        if row:
            return dict(row)

        # 2) 部分一致: 案件の機関名が agencies.name に含まれる or 逆
        row = conn.execute(
            "SELECT * FROM agencies WHERE name LIKE ? OR ? LIKE '%' || name || '%' "
            "ORDER BY njss_count DESC LIMIT 1",
            (f"%{agency_name}%", agency_name),
        ).fetchone()
        if row:
            return dict(row)

        # 3) 官公需API形式 "都道府県名+市町村名" → 市町村名で検索
        m = re.search(r'[都道府県](.+?[市町村区])$', agency_name)
        if m:
            city = m.group(1)
            row = conn.execute(
                "SELECT * FROM agencies WHERE name LIKE ? ORDER BY njss_count DESC LIMIT 1",
                (f"%{city}%",),
            ).fetchone()
            if row:
                return dict(row)

        # 4) 省庁名の先頭キーワードでマッチ（4文字以上のときのみ＝誤マッチ防止）
        keyword = agency_name.split("／")[0].split(" ")[0][:8]
        if len(keyword) >= 4:
            row = conn.execute(
                "SELECT * FROM agencies WHERE name LIKE ? ORDER BY njss_count DESC LIMIT 1",
                (f"%{keyword}%",),
            ).fetchone()
            if row:
                return dict(row)

    return None


CSV_COLUMNS = ["id", "source", "prefecture", "region", "agency", "agency_type",
               "title", "category", "bid_method", "announced_date", "deadline",
               "budget", "spec_status", "spec_reason", "winner", "win_price",
               "detail_url", "external_id"]


def export_cases_csv() -> str:
    """全案件を CSV 文字列にして返す（強化済みDBの書き出し用）。"""
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(CSV_COLUMNS)
    with _connect() as conn:
        for r in conn.execute(
            f"SELECT {', '.join(CSV_COLUMNS)} FROM cases "
            f"ORDER BY prefecture, announced_date DESC"
        ).fetchall():
            w.writerow([r[c] for c in CSV_COLUMNS])
    return out.getvalue()


def clear_cases(source: str | None = None) -> int:
    """案件を削除（source指定でその取得元のみ）。削除件数を返す。

    関連する applications も case 削除で連鎖（ON DELETE CASCADE 相当）するよう
    手動で掃除する。
    """
    with _connect() as conn:
        if source:
            n = conn.execute("DELETE FROM cases WHERE source = ?", (source,)).rowcount
        else:
            n = conn.execute("DELETE FROM cases").rowcount
        # 孤立した applications を掃除
        conn.execute("DELETE FROM applications WHERE case_id NOT IN (SELECT id FROM cases)")
        conn.commit()
    return n


# ============================================================
# 入札参加申請（applications）
# ============================================================

def _normalize_cost_items(items: Any) -> str:
    """自社原価の内訳を検証してJSON文字列にする。

    各行 {label, amount(円)}。項目名も金額も空の行は捨てる。
    """
    import json
    if isinstance(items, str):
        try:
            items = json.loads(items or "[]")
        except (ValueError, TypeError):
            items = []
    if not isinstance(items, list):
        items = []
    cleaned: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        label = str(it.get("label", "") or "").strip()
        amount = yen_to_int(str(it.get("amount", "") or "")) or 0
        if label or amount:
            cleaned.append({"label": label, "amount": amount})
    return json.dumps(cleaned, ensure_ascii=False)


def _normalize_partners(partners: Any) -> str:
    """協力会社見積(quotes)を検証してJSON文字列にする。

    各社 {company, tel, area, amount, requested, replied, feasible, selected, note}。
    会社名が空のものは捨てる。常に妥当なJSON配列文字列を返す。
    """
    import json
    if isinstance(partners, str):
        try:
            items = json.loads(partners or "[]")
        except (ValueError, TypeError):
            items = []
    elif isinstance(partners, list):
        items = partners
    else:
        items = []
    cleaned: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        company = str(it.get("company", "")).strip()
        if not company:
            continue
        # feasible は3状態: 1=可 / -1=否(断り) / 0=未回答（bid-next の可否UIに合わせる）
        fe = it.get("feasible")
        feasible = 1 if fe in (1, "1", "yes", True) else (-1 if fe in (-1, "-1", "no") else 0)
        cleaned.append({
            "company": company,
            "tel": str(it.get("tel", "")).strip(),
            "area": str(it.get("area", "")).strip(),
            "amount": str(it.get("amount", "")).strip(),
            "requested": 1 if it.get("requested") else 0,
            "replied": 1 if it.get("replied") else 0,
            "feasible": feasible,
            "selected": 1 if it.get("selected") else 0,
            "note": str(it.get("note", "")).strip(),
        })
    return json.dumps(cleaned, ensure_ascii=False)


# set_application が受け付ける列（case_id/status 以外）。型変換つき。
_APP_TEXT_FIELDS = (
    "applied_date", "note", "assignee", "apply_deadline", "bid_deadline",
    "open_date", "submit_method", "work", "materials", "flag", "partner",
    "agency_override",  # 元機関(発注機関)の手書き上書き（案件のagencyが不正確な時に修正）
    "win_company",      # 落札会社名（自社・他社問わず最終落札先）
    "inquiry_period",   # 質疑（質問書）受付期間（要望⑱-①）
)
_APP_INT_FIELDS = ("needs_check", "bid_plan", "win_amount", "award_called")


def delete_application(case_id: int) -> None:
    """案件を申請管理から外す（applications行を削除）。案件(cases)自体は残す。"""
    with _connect() as conn:
        conn.execute("DELETE FROM applications WHERE case_id = ?", (case_id,))
        conn.commit()
    _push_applications()


# ============================================================
# Supabase 永続化（write-through ＋ 起動時復元）
# ============================================================

def _applications_for_supa() -> list[dict[str, Any]]:
    """全申請を external_id つきで取り出す（Supabase保存用・案件ID振り直しに強い）。"""
    sql = """
        SELECT c.external_id AS external_id,
               c.title AS _case_title, c.agency AS _case_agency,
               COALESCE(NULLIF(c.sector, ''), '公共') AS _case_sector,
               c.source AS _case_source, c.category AS _case_category,
               a.*
        FROM applications a JOIN cases c ON c.id = a.case_id
        WHERE c.external_id IS NOT NULL AND c.external_id != ''
    """
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(sql).fetchall()]
    out = []
    for r in rows:
        r.pop("case_id", None)
        r = _hydrate_application(r)  # partners を list に
        out.append(r)
    return out


# Supabase に入っている申請の件数（起動時の復元で判明した値／push成功のたびに更新）。
# 「いきなり大幅に減る書き戻し」を検知するための基準。
_supa_app_count: int | None = None

# ============================================================
# 【不変条件】読み込めていないものを、書き戻さない
# ============================================================
# Supabase が真の保存先で、SQLite は毎デプロイで作り直される。
# 各 _push_*() は「今SQLiteにある全件」で丸ごと上書きするため、
# **起動時の復元に失敗したキーをそのまま書き戻すと、保存済みの内容が消える。**
#
# 実際の危険（2026-08-18の点検で判明）:
#   復元は1つの try で全キーをまとめて処理していたため、途中で例外が出ると
#   それ以降（マイ条件・監視機関の除外・NG集計の記録・AI使用量）が**丸ごと未復元**に
#   なる。その状態で利用者が1操作すると、空のまま上書きされて永久に失われる。
#   AI使用量は請求根拠なので、消えると請求できなくなる。
#
# そこで、キーごとに復元結果を記録し、
#   loaded … サーバから読めて反映できた      → 書き戻してよい
#   empty  … サーバに元々何も無かった        → 書き戻してよい（失うものが無い）
#   failed … 読めなかった／反映に失敗した    → **書き戻さない**
# とする。判断に迷ったら書かない（消えるより残るほうが良い）。
_restore_state: dict[str, str] = {}

# 画面に出す日本語名（利用者に「何が保存されなかったか」を伝えるため）
_KEY_LABELS = {
    "applications": "申請管理",
    "companies": "協力会社",
    "profile": "マイ条件",
    "agency_exclusions": "監視機関の除外設定",
    "ng_reports": "NG集計の記録",
    "ai_usage": "AI使用量（請求根拠）",
}


def _may_push(key: str) -> bool:
    """このキーを書き戻してよいか。復元できていないものは書かせない。"""
    if not supa.enabled():
        return False
    st = _restore_state.get(key)
    if st in ("loaded", "empty"):
        return True
    supa.block_save(
        f"「{_KEY_LABELS.get(key, key)}」の保存を見送りました。"
        "起動時にサーバから読み込めていないため、いま保存すると"
        "保存済みの内容を消してしまいます。時間をおいて画面を再読み込みしてください。")
    return False


def _mark_restored(key: str, loaded: bool) -> None:
    _restore_state[key] = "loaded" if loaded else "empty"


def _restore_section(key: str, fn) -> int:
    """復元を1キー分だけ実行する。**1つ失敗しても他を巻き添えにしない。**

    以前は全キーが1つの try に入っていたため、最初の失敗で以降が全部未復元になり、
    そのまま書き戻すと消える、という連鎖事故の形になっていた。
    """
    try:
        n = fn()
        return n
    except Exception as e:  # noqa: BLE001
        _restore_state[key] = "failed"
        import logging
        logging.getLogger(__name__).warning("supa restore %s failed: %s", key, e)
        return 0


def _push_applications() -> None:
    """SQLiteの申請をSupabaseへ書き戻す（Supabaseが真の保存先）。

    【データ消失防止】丸ごと上書きなので、ここが痩せた内容で走るとお客様の入力が
    永久に消える。2026-08-17の事故はまさにその一歩手前だった。二重に守る:
      1. 復元できなかった申請(_unlinked_apps)を必ず混ぜる ＝ そもそも減らさない
      2. それでも大幅に減るときは書き戻しを中止して警告 ＝ 最後の砦
    """
    global _supa_app_count
    if _restoring or not _may_push("applications"):
        return
    rows = _applications_for_supa()

    # 退避してある申請（案件が見つからず復元できなかったぶん）を混ぜ戻す。
    if _unlinked_apps:
        have = {r.get("external_id") for r in rows}
        rows += [it for it in _unlinked_apps
                 if (it.get("external_id") or "") not in have]

    # 【最後の砦】1件ずつの削除は正常だが、一度に大きく減るのは異常。
    # 案件データの不具合や復元漏れが原因のことが多く、放置すると永久消失になる。
    prev = _supa_app_count
    if prev is not None and prev >= 10 and len(rows) < prev * 0.7:
        supa.block_save(
            f"申請の書き戻しを中止しました（{prev}件 → {len(rows)}件へ急減）。"
            "案件データの不具合で復元しきれていない可能性があります。"
            "この状態で保存するとお客様の入力が失われるため、あえて保存していません。")
        return

    if supa.save("applications", rows):
        _supa_app_count = len(rows)


def _push_companies() -> None:
    if _restoring or not _may_push("companies"):
        return
    supa.save("companies", list_companies())


def _push_profile() -> None:
    if _restoring or not _may_push("profile"):
        return
    with _connect() as conn:
        row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    if row:
        supa.save("profile", _hydrate_profile(dict(row)))


def _push_exclusions() -> None:
    if _restoring or not _may_push("agency_exclusions"):
        return
    supa.save("agency_exclusions", sorted(list_agency_exclusions()))


def _push_ng_reports() -> None:
    if _restoring or not _may_push("ng_reports"):
        return
    supa.save("ng_reports", list_ng_reports())


def restore_from_supa() -> dict[str, int]:
    """起動時：Supabaseの保存内容を SQLite へ流し込む（Supabaseが真の保存先）。

    すべて失敗しても例外は投げない（アプリは動き続ける）。
    """
    global _restoring, _supa_app_count
    if not supa.enabled():
        return {}
    counts = {"applications": 0, "companies": 0, "profile": 0, "exclusions": 0}
    _restoring = True
    try:
        # 申請（external_id → 現在の case_id に解決して投入）
        apps = _restore_section("applications", lambda: supa.load("applications"))
        if not isinstance(apps, list) and _restore_state.get("applications") != "failed":
            # 読めたが中身が無い＝サーバに元々保存が無い。書き戻して失うものは無い。
            _mark_restored("applications", False)
        if isinstance(apps, list):
            _mark_restored("applications", bool(apps))
            # 【復旧の保険】読み込めた「正常な状態」を、何かに上書きされる前に
            # 日付つきで控えておく。起動ごとに1回だけなので負荷は無視できる。
            # 万一また消えても、ここから戻せる（今回はこれが無くて肝を冷やした）。
            if apps:
                _supa_app_count = len(apps)
                try:
                    import datetime as _dt
                    supa.save(f"applications_bak_{_dt.date.today():%Y%m%d}", apps)
                except Exception:  # noqa: BLE001
                    # 控えを取れなくても復元は必ず続ける。保険の失敗で
                    # 本体（お客様データの復元）を止めては本末転倒。
                    pass
            for it in apps:
                ext = (it.get("external_id") or "").strip()
                if not ext:
                    continue
                cid = get_case_id_by_external(ext)
                if cid is None:
                    # 手動案件は案件本体ごと再生成（揮発DB対策）。公共のスクレイプ案件は
                    # 公開終了とみなしスキップ（従来どおり）。
                    if ext.startswith("manual:"):
                        upsert_cases([{
                            "source": it.get("_case_source") or "manual",
                            "external_id": ext,
                            "title": it.get("_case_title") or "（無題の案件）",
                            "agency": it.get("_case_agency") or "",
                            "category": it.get("_case_category") or "",
                            "sector": it.get("_case_sector") or "民間",
                        }])
                        cid = get_case_id_by_external(ext)
                if cid is None:
                    # 【データ消失防止】案件が見つからないだけで、お客様の入力を
                    # 捨ててはいけない。退避しておき、書き戻すときに必ず混ぜる。
                    # 案件データが直れば次の起動で正しく復元される。
                    _unlinked_apps.append(it)
                    continue
                fields = {k: it.get(k) for k in (
                    "applied_date", "note", "assignee", "apply_deadline", "bid_deadline",
                    "open_date", "submit_method", "work", "materials", "flag",
                    "needs_check", "bid_plan", "win_amount", "award_called", "partner", "partners",
                    # 以下が欠けると再起動のたびに落札会社名・原価内訳・機関上書き・
                    # 編集世代が消える（保存ロールバックの一因だった）
                    "win_company", "cost_items", "agency_override", "client_mtime",
                    "inquiry_period")}
                try:
                    set_application(cid, it.get("status") or "参加申請準備前", **fields)
                    # 仕様書の紐付けは set_application では書かないので個別に復元（要望⑦STEP1）
                    sf = it.get("spec_files")
                    if isinstance(sf, list) and sf:
                        set_spec_files(cid, sf)
                    counts["applications"] += 1
                except ValueError:
                    pass
        # 協力会社（全消し→投入）。空/欠損のときは消さない（誤って全消しする事故を防ぐ）。
        def _r_companies() -> int:
            comps = supa.load("companies")
            if not isinstance(comps, list) or not comps:
                _mark_restored("companies", False)
                return 0
            with _connect() as conn:
                conn.execute("DELETE FROM companies")
                conn.commit()
            n = 0
            for c in comps:
                c.pop("id", None)
                upsert_company(c)
                n += 1
            _mark_restored("companies", True)
            return n
        counts["companies"] += _restore_section("companies", _r_companies)
        # マイ条件
        def _r_profile() -> int:
            prof = supa.load("profile")
            if not isinstance(prof, dict) or not (prof.get("company") or prof.get("qualifications")):
                _mark_restored("profile", False)
                return 0
            save_profile(
                prefectures=prof.get("prefectures", ""),
                categories=prof.get("categories", "電気工事"),
                budget_max=prof.get("budget_max", ""),
                grade=prof.get("grade", ""), quals=prof.get("quals", ""),
                company=prof.get("company", ""), representative=prof.get("representative", ""),
                address=prof.get("address", ""), corp_number=prof.get("corp_number", ""),
                qualifications=prof.get("qualifications", []))
            _mark_restored("profile", True)
            return 1
        counts["profile"] = _restore_section("profile", _r_profile)
        # 監視機関の除外。空/欠損のときは置換しない（既存を消さない）。
        def _r_exclusions() -> int:
            exc = supa.load("agency_exclusions")
            if not isinstance(exc, list) or not exc:
                _mark_restored("agency_exclusions", False)
                return 0
            replace_agency_exclusions(exc)
            _mark_restored("agency_exclusions", True)
            return len(exc)
        counts["exclusions"] = _restore_section("agency_exclusions", _r_exclusions)
        # NG集計の保存記録。空/欠損のときは消さない（既存を守る）。
        def _r_ng_reports() -> int:
            reps = supa.load("ng_reports")
            if not isinstance(reps, list) or not reps:
                _mark_restored("ng_reports", False)
                return 0
            with _connect() as conn:
                conn.execute("DELETE FROM ng_reports")
                # list_ng_reports は新しい順で保存しているので、古い順に挿入し直す
                for r in reversed(reps):
                    if not isinstance(r, dict) or not r.get("payload"):
                        continue
                    conn.execute(
                        "INSERT INTO ng_reports (created_at, sheet, payload)"
                        " VALUES (?, ?, ?)",
                        (r.get("created_at") or "", r.get("sheet") or "公共",
                         r["payload"]))
                conn.commit()
            _mark_restored("ng_reports", True)
            return len(reps)
        counts["ng_reports"] = _restore_section("ng_reports", _r_ng_reports)

        # AI使用量の月次集計。空/欠損のときは消さない（請求根拠を守る）。
        def _r_ai_usage() -> int:
            usage = supa.load("ai_usage")
            if not isinstance(usage, list) or not usage:
                _mark_restored("ai_usage", False)
                return 0
            with _connect() as conn:
                conn.execute("DELETE FROM ai_usage")
                for u in usage:
                    if not isinstance(u, dict) or not u.get("month"):
                        continue
                    conn.execute(
                        """INSERT OR REPLACE INTO ai_usage
                           (month, kind, model, calls, prompt_tokens, output_tokens, taps)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (u.get("month"), u.get("kind") or "その他", u.get("model") or "",
                         int(u.get("calls") or 0), int(u.get("prompt_tokens") or 0),
                         int(u.get("output_tokens") or 0), int(u.get("taps") or 0)))
                conn.commit()
            _mark_restored("ai_usage", True)
            return len(usage)
        counts["ai_usage"] = _restore_section("ai_usage", _r_ai_usage)
    except Exception as e:  # noqa: BLE001 — 復元失敗でアプリを落とさない
        import logging
        logging.getLogger(__name__).warning("supa restore failed: %s", e)
    finally:
        _restoring = False
    return counts


def set_application(case_id: int, status: str, **fields: Any) -> None:
    """案件の入札参加申請ステータスと管理項目を登録・更新する。

    fields には _APP_TEXT_FIELDS / _APP_INT_FIELDS と partners(quotes) を渡せる。
    未指定の列はデフォルト（空文字 / 0 / '[]'）になる。
    """
    status = normalize_status(status)
    if status not in APP_STATUSES_ALL:
        raise ValueError(f"不正なステータス: {status}")

    def _int(v: Any) -> int:
        if isinstance(v, bool):
            return 1 if v else 0
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    cols = ["status"]
    vals: list[Any] = [status]
    for f in _APP_TEXT_FIELDS:
        cols.append(f)
        vals.append(str(fields.get(f, "") or "").strip())
    for f in _APP_INT_FIELDS:
        cols.append(f)
        vals.append(_int(fields.get(f, 0)))
    cols.append("partners")
    vals.append(_normalize_partners(fields.get("partners", [])))
    cols.append("cost_items")
    vals.append(_normalize_cost_items(fields.get("cost_items", [])))
    # client_mtime は渡された時だけ更新する（未指定の保存経路で 0 に戻さない）
    if "client_mtime" in fields:
        cols.append("client_mtime")
        vals.append(_int(fields.get("client_mtime", 0)))

    set_clause = ", ".join(f"{c}=excluded.{c}" for c in cols)
    placeholders = ", ".join(["?"] * (len(cols) + 1))  # +case_id
    with _connect() as conn:
        conn.execute(
            f"""INSERT INTO applications (case_id, {', '.join(cols)}, updated_at)
                VALUES ({placeholders}, datetime('now'))
                ON CONFLICT(case_id) DO UPDATE SET
                  {set_clause}, updated_at=datetime('now')""",
            (case_id, *vals),
        )
        conn.commit()
    _push_applications()


def _hydrate_application(row: dict[str, Any]) -> dict[str, Any]:
    """DB行の partners / spec_files(JSON文字列) を list に展開して返す。"""
    import json
    try:
        row["partners"] = json.loads(row.get("partners") or "[]")
    except (ValueError, TypeError):
        row["partners"] = []
    try:
        row["spec_files"] = json.loads(row.get("spec_files") or "[]")
    except (ValueError, TypeError):
        row["spec_files"] = []
    try:
        row["cost_items"] = json.loads(row.get("cost_items") or "[]")
    except (ValueError, TypeError):
        row["cost_items"] = []
    return row


# ---- 仕様書の紐付け（要望⑦STEP1） -----------------------------------------
# spec_files は set_application では触らず専用APIで更新する（通常保存で消えないように）。

def get_spec_files(case_id: int) -> list[dict[str, Any]]:
    """案件に紐付いた仕様書リスト（[{name,kind,url|key,size}])。"""
    import json
    with _connect() as conn:
        row = conn.execute(
            "SELECT spec_files FROM applications WHERE case_id = ?", (case_id,)
        ).fetchone()
    if not row:
        return []
    try:
        return json.loads(row[0] or "[]")
    except (ValueError, TypeError):
        return []


def set_spec_files(case_id: int, files: list[dict[str, Any]]) -> None:
    """仕様書リストを保存。applications 行が無ければ既定ステータスで作る。"""
    import json
    payload = json.dumps(files or [], ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """INSERT INTO applications (case_id, status, spec_files, updated_at)
               VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(case_id) DO UPDATE SET spec_files=excluded.spec_files,
                 updated_at=datetime('now')""",
            (case_id, "参加申請準備前", payload),
        )
        conn.commit()
    _push_applications()


def save_spec_blob(key: str, name: str, mime: str, data: bytes) -> None:
    """仕様書ファイルの実体を保存（SQLite＋Supabaseへbase64でミラー＝揮発対策）。"""
    import base64
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO spec_blobs (key, mime, name, data) VALUES (?, ?, ?, ?)",
            (key, mime, name, data),
        )
        conn.commit()
    try:  # 永続化（未設定/不通でも黙って続行）
        supa.save("specblob:" + key,
                  {"mime": mime, "name": name,
                   "b64": base64.b64encode(data).decode("ascii")})
    except Exception:  # noqa: BLE001
        pass


def get_spec_blob(key: str) -> tuple[str, str, bytes] | None:
    """仕様書ファイルの実体を取得 (mime, name, bytes)。SQLite→Supabaseの順。"""
    import base64
    with _connect() as conn:
        row = conn.execute(
            "SELECT mime, name, data FROM spec_blobs WHERE key = ?", (key,)
        ).fetchone()
    if row and row[2] is not None:
        return (row[0] or "", row[1] or "", bytes(row[2]))
    obj = None
    try:
        obj = supa.load("specblob:" + key)
    except Exception:  # noqa: BLE001
        obj = None
    if isinstance(obj, dict) and obj.get("b64"):
        try:
            data = base64.b64decode(obj["b64"])
        except Exception:  # noqa: BLE001
            return None
        # SQLiteへ書き戻して次回以降を速く（揮発復元）
        try:
            save_spec_blob(key, obj.get("name", ""), obj.get("mime", ""), data)
        except Exception:  # noqa: BLE001
            pass
        return (obj.get("mime", ""), obj.get("name", ""), data)
    return None


def get_application(case_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM applications WHERE case_id = ?", (case_id,)
        ).fetchone()
        return _hydrate_application(dict(row)) if row else None


def list_applications(status: str | None = None) -> list[dict[str, Any]]:
    """申請管理一覧（案件情報をJOINして返す）。新しい更新順。"""
    sql = """
        SELECT a.*,
               c.title,
               COALESCE(NULLIF(a.agency_override, ''), c.agency) AS agency,
               c.agency_type, c.region, c.prefecture,
               c.category, c.deadline, c.announced_date, c.detail_url,
               c.external_id, c.budget, c.winner, c.win_price, c.spec_status,
               COALESCE(NULLIF(c.sector, ''), '公共') AS sector, c.source
        FROM applications a
        JOIN cases c ON c.id = a.case_id
    """
    params: list[Any] = []
    if status:
        sql += " WHERE a.status = ?"
        params.append(status)
    sql += " ORDER BY a.updated_at DESC"
    with _connect() as conn:
        return [_hydrate_application(dict(r)) for r in conn.execute(sql, params).fetchall()]


# ============================================================
# 協力会社マスタ（companies）
# ============================================================

def _hydrate_company(row: dict[str, Any]) -> dict[str, Any]:
    import json
    for k in ("tags", "reviews"):
        try:
            row[k] = json.loads(row.get(k) or "[]")
        except (ValueError, TypeError):
            row[k] = []
    row["partner"] = bool(row.get("partner"))
    row["sector"] = row.get("sector") or "公共"
    return row


def list_companies() -> list[dict[str, Any]]:
    """協力会社一覧（★よく頼む→評価の高い順）。"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM companies ORDER BY partner DESC, rating DESC, name ASC"
        ).fetchall()
    return [_hydrate_company(dict(r)) for r in rows]


def upsert_company(data: dict[str, Any]) -> int:
    """協力会社を登録／更新する。id があれば更新。会社IDを返す。"""
    import json
    cid = data.get("id")
    vals = (
        str(data.get("name", "")).strip(),
        str(data.get("area", "")).strip(),
        json.dumps([t for t in (data.get("tags") or []) if t], ensure_ascii=False),
        str(data.get("tel", "")).strip(),
        str(data.get("url", "")).strip(),
        str(data.get("note", "")).strip(),
        1 if data.get("partner") else 0,
        int(data.get("rating") or 0),
        json.dumps([r for r in (data.get("reviews") or []) if r], ensure_ascii=False),
        # 区分（公共/民間）。協力業者リストは公共・民間で分けて管理する。
        ("民間" if str(data.get("sector", "")).strip() == "民間" else "公共"),
    )
    with _connect() as conn:
        if cid:
            conn.execute(
                """UPDATE companies SET name=?, area=?, tags=?, tel=?, url=?,
                   note=?, partner=?, rating=?, reviews=?, sector=? WHERE id=?""",
                (*vals, int(cid)),
            )
            conn.commit()
            ret = int(cid)
        else:
            cur = conn.execute(
                """INSERT INTO companies (name, area, tags, tel, url, note, partner, rating, reviews, sector)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", vals,
            )
            conn.commit()
            ret = int(cur.lastrowid)
    _push_companies()
    return ret


def delete_company(company_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM companies WHERE id=?", (company_id,))
        conn.commit()
    _push_companies()


def count_companies() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]


# ============================================================
# 競合企業（落札者の分析）
# ============================================================

def normalize_company(name: str) -> str:
    """会社名を集計用に正規化する（表記ゆれを吸収して精度を上げる）。

    例: 「(株)山田電気」「株式会社 山田電気」「山田電気㈱」→「山田電気」
    """
    import re
    n = name.strip()
    # 法人格表記を除去
    n = re.sub(r"株式会社|有限会社|合同会社|\(株\)|（株）|㈱|\(有\)|（有）|㈲", "", n)
    # 空白・全角空白を除去
    n = re.sub(r"[\s　]+", "", n)
    return n


def list_competitors(q: str = "", prefecture: str = "",
                     prefectures: list[str] | None = None,
                     exclude_company: str = "") -> list[dict[str, Any]]:
    """落札者を企業ごとに集計（落札件数の多い順）。

    自社の競合を見るため:
      - prefectures: 自社の対応エリア（複数）に絞る
      - exclude_company: 自社名を一覧から除外（表記ゆれ吸収）
    prefecture（単数）は手動の追加絞り込み用。
    """
    where = ["winner != ''"]
    params: list[Any] = []
    if prefectures:
        where.append("prefecture IN (%s)" % ",".join("?" * len(prefectures)))
        params.extend(prefectures)
    if prefecture:
        where.append("prefecture = ?")
        params.append(prefecture)
    clause = "WHERE " + " AND ".join(where)
    sql = f"""
        SELECT winner,
               COUNT(*)                          AS wins,
               COUNT(DISTINCT prefecture)        AS pref_count,
               GROUP_CONCAT(DISTINCT prefecture) AS prefectures,
               GROUP_CONCAT(DISTINCT agency)     AS agencies
        FROM cases {clause}
        GROUP BY winner
        ORDER BY wins DESC, winner ASC
    """
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if exclude_company:
        ex = normalize_company(exclude_company)
        rows = [r for r in rows if normalize_company(r["winner"]) != ex]
    if q:
        nq = normalize_company(q)
        rows = [r for r in rows if nq in normalize_company(r["winner"])]
    return rows


def competitor_cases(winner: str) -> list[dict[str, Any]]:
    """指定した落札者（競合企業）の落札案件一覧。"""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM cases WHERE winner = ? ORDER BY announced_date DESC", (winner,)
        ).fetchall()]


# ============================================================
# マイ条件（プロフィール）とマッチング
# ============================================================

def yen_to_int(s: str) -> int | None:
    """「166,518,000円」等を整数に。数字が無ければ None。"""
    import re
    digits = re.sub(r"[^\d]", "", s or "")
    return int(digits) if digits else None


def _hydrate_profile(row: dict[str, Any]) -> dict[str, Any]:
    import json
    try:
        row["qualifications"] = json.loads(row.get("qualifications") or "[]")
    except (ValueError, TypeError):
        row["qualifications"] = []
    for k in ("representative", "address", "corp_number"):
        row.setdefault(k, "")
    return row


# 初期値（資格通知書PDFから抽出した川野電気の発注機関別 等級）。
# 本番DBは毎日再生成され profile 行が消えるため、未設定時はこの既定値を表示・AI照合に使う。
# (issuer, (elecCat,grade,score), (pipeCat,grade,score), valid, number, note)
_DEFAULT_QUAL_MAIN = [
    ("国土交通省（建設工事）", ("電気工事", "B", "621"), ("管工事", "B", "559"), "令和9年3月31日", "整理070100053", "国交省の建設工事資格"),
    ("国土交通省（地方整備局・官庁営繕／一元化資格）", ("電気設備工事", "C", "621"), ("暖冷房衛生工事", "C", "559"), "令和9年3月31日", "業者86-0306424", "関東/近畿/中部/中国/四国/九州/東北/北陸整備局・国総研・営繕で共通。維持修繕/受変電/橋梁補修=532"),
    ("法務省", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "登録60667", "大臣官房施設課"),
    ("財務省 東海財務局", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "受付770045", "東海財務局/名古屋税関/名古屋国税局"),
    ("財務省 福岡財務局", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "受付120069", "福岡財務支局/門司・長崎税関/福岡国税局"),
    ("財務省 中国財務局", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "受付700169", "中国財務局/広島国税局"),
    ("財務省 四国財務局", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "受付875544", "四国財務局/高松国税局"),
    ("財務省 関東財務局", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "受付130168", "財務省本省/関東財務局/東京・横浜税関/国税庁/東京・関東信越国税局。関財会第716号"),
    ("財務省 九州財務局", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "受付700174", "九州財務局/熊本国税局。九財統国3第128号"),
    ("財務省 東北財務局", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "受付120038", "東北財務局/仙台国税局。仙財会第443号"),
    ("財務省 北海道財務局", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "受付410110", "北海道財務局/札幌国税局/函館税関。北海財国管1第312号"),
    ("文部科学省", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和7・8年度", "受付770819", "文教施設企画・防災部"),
    ("厚生労働省", ("電気", "D", "621"), ("管", "D", "559"), "令和9年3月31日", "登録115-260059", "全国ブロック。※C相当点でもD等級"),
    ("環境省", ("電気設備工事", "C", "621"), ("機械設備工事", "C", "559"), "令和9年3月31日", "登録001-000448", "全国"),
    ("総務省", ("電気", "C", "621"), ("管", "C", "559"), "令和9年3月31日", "受付0700101545", ""),
    ("農林水産省（大臣官房）", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "登録00451", ""),
    ("経済産業省（近畿経済産業局）", ("電気", "C", "621"), ("管", "C", "559"), "令和9年3月31日", "登録0803170079", "全国地区"),
    ("防衛省", ("電気", "C", "621"), ("管", "C", "559"), "令和9年3月31日", "登録2-06-04701", "整備計画局"),
    ("沖縄総合事務局", ("電気設備工事", "C", "621"), ("暖冷房衛生設備工事", "C", "559"), "令和9年3月31日", "業者180571", "受変電532"),
    ("衆議院", ("電気工事", "C", "621"), ("管工事", "C", "559"), "令和9年3月31日", "登録3125", ""),
    ("参議院", ("電気工事", "C", ""), ("管工事", "C", ""), "令和9年3月31日", "登録1562", ""),
    ("最高裁判所", ("電気", "C", "621"), ("管", "C", "559"), "令和9年3月31日", "業者0062555/受付16889", ""),
    ("独立行政法人 都市再生機構（UR）", ("電気工事", "C", "621"), ("管工事", "C", "559"), "2027年3月31日", "登録0217290", "関西地区"),
    ("独立行政法人 水資源機構", ("電気", "", "621"), ("管", "", "559"), "令和9年3月31日", "業者152064", "等級表記○/順位3381"),
    ("大阪府", ("電気工事", "D", "692"), ("管工事", "D", "593"), "令和9年3月31日", "業者7094979", "等級・点数は令和8/3/31まで有効"),
    ("八尾市", ("電気工事", "D", "592"), ("管工事", "D", "493"), "令和7年度", "市内業者", "格付表"),
]
_DEFAULT_QUAL_NOGRADE = [
    ("農林水産省 各地方農政局", "令和9年3月31日", "登録20250C…", "関東/近畿/九州/中国四国/東海/東北/北陸。電気・管=有資格（格付なし）"),
    ("林野庁（近畿中国森林管理局）", "令和9年3月31日", "登録N09865", "電気・管=有資格（格付なし）"),
    ("外務省", "令和9年3月31日", "登録070800100678", "電気・管=有資格（格付なし）"),
]


def default_qualifications() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for issuer, e, m, valid, num, note in _DEFAULT_QUAL_MAIN:
        out.append({"issuer": issuer, "category": e[0], "grade": e[1], "score": e[2],
                    "valid_until": valid, "number": num, "note": note})
        out.append({"issuer": issuer, "category": m[0], "grade": m[1], "score": m[2],
                    "valid_until": valid, "number": num, "note": ""})
    for issuer, valid, num, note in _DEFAULT_QUAL_NOGRADE:
        out.append({"issuer": issuer, "category": "電気工事/管工事", "grade": "", "score": "",
                    "valid_until": valid, "number": num, "note": note})
    return out


def default_profile() -> dict[str, Any]:
    """資格通知書ベースの初期プロフィール（profile行が無いとき表示・AI照合に使う）。"""
    return {
        "id": 1, "company": "川野電気（株）", "representative": "川野 善輝",
        "address": "〒581-0039 大阪府八尾市太田新町8-29", "corp_number": "7122001031468",
        "prefectures": "大阪府,兵庫県,京都府,奈良県,和歌山県,滋賀県",
        "categories": "電気工事", "budget_max": "",
        "grade": "経審 電気621/管559（全国基準。等級は機関で異なる）",
        "quals": "建設業許可（電気工事業）,第一種電気工事士,経営事項審査（経審）,入札参加資格登録",
        "qualifications": default_qualifications(),
    }


def get_profile() -> dict[str, Any]:
    """マイ条件を取得。profile行が無い／等級が空なら資格通知書ベースの初期値を補う（揮発DB対策）。"""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    if not row:
        return default_profile()
    p = _hydrate_profile(dict(row))
    # 古いlocalStorage復元などで等級が空のまま行が出来ているケース → 初期値を補完。
    if not p.get("qualifications"):
        d = default_profile()
        p["qualifications"] = d["qualifications"]
        for k in ("company", "representative", "address", "corp_number", "grade"):
            if not p.get(k):
                p[k] = d[k]
    return p


def _normalize_qualifications(quals: Any) -> str:
    """機関別の等級を検証してJSON文字列にする。各行 {issuer, category, grade, score, valid_until, number, note}。"""
    import json
    if isinstance(quals, str):
        try:
            items = json.loads(quals or "[]")
        except (ValueError, TypeError):
            items = []
    elif isinstance(quals, list):
        items = quals
    else:
        items = []
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        issuer = str(it.get("issuer", "")).strip()
        if not issuer:
            continue
        out.append({k: str(it.get(k, "")).strip() for k in
                    ("issuer", "category", "grade", "score", "valid_until", "number", "note")})
    return json.dumps(out, ensure_ascii=False)


def save_profile(prefectures: str, categories: str, budget_max: str,
                 grade: str = "", quals: str = "", company: str = "",
                 representative: str = "", address: str = "",
                 corp_number: str = "", qualifications: Any = None) -> None:
    """マイ条件を保存（単一行 upsert）。"""
    quals_json = _normalize_qualifications(qualifications if qualifications is not None else [])
    with _connect() as conn:
        conn.execute(
            """INSERT INTO profile
                 (id, company, prefectures, categories, budget_max, grade, quals,
                  representative, address, corp_number, qualifications, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                 company=excluded.company, prefectures=excluded.prefectures,
                 categories=excluded.categories, budget_max=excluded.budget_max,
                 grade=excluded.grade, quals=excluded.quals,
                 representative=excluded.representative, address=excluded.address,
                 corp_number=excluded.corp_number, qualifications=excluded.qualifications,
                 updated_at=datetime('now')""",
            (company, prefectures, categories, budget_max, grade, quals,
             representative, address, corp_number, quals_json),
        )
        conn.commit()
    _push_profile()


def match_cases(profile: dict[str, Any], limit: int = 300) -> list[dict[str, Any]]:
    """マイ条件に合致する案件を、マッチ理由つきで返す（公告が新しい順）。

    マッチ条件:
      - 対応エリア（都道府県）に含まれる
      - 対応業種に category が含まれる（部分一致: 「電気」を含む等）
      - 予算上限が設定されていれば 予定価格 ≤ 上限
    """
    prefs = [p.strip() for p in (profile.get("prefectures") or "").split(",") if p.strip()]
    cats = [c.strip() for c in (profile.get("categories") or "").split(",") if c.strip()]
    budget_max = yen_to_int(profile.get("budget_max") or "")
    if not prefs and not cats:
        return []

    where, params = [], []
    if prefs:
        where.append("prefecture IN (%s)" % ",".join("?" * len(prefs)))
        params.extend(prefs)
    if cats:
        where.append("(" + " OR ".join("category LIKE ?" for _ in cats) + ")")
        params.extend(f"%{c}%" for c in cats)
    clause = "WHERE " + " AND ".join(where) if where else ""
    sql = (f"SELECT * FROM cases {clause} "
           f"ORDER BY CASE WHEN announced_date='' THEN 1 ELSE 0 END, announced_date DESC LIMIT ?")
    params.append(limit)
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    out = []
    for r in rows:
        price = yen_to_int(r.get("budget") or "")
        if budget_max and price and price > budget_max:
            continue
        reasons = []
        if r.get("prefecture") in prefs:
            reasons.append(f"対応エリア（{r['prefecture']}）")
        if any(c in (r.get("category") or "") for c in cats):
            reasons.append(f"業種一致（{r['category']}）")
        if budget_max and price:
            reasons.append("予算内")
        r["match_reasons"] = reasons
        out.append(r)
    return out
