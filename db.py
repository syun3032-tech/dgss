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

import sqlite3
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).parent / "denki_bid.db"

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
    created_at    TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cases_pref     ON cases(prefecture);
CREATE INDEX IF NOT EXISTS idx_cases_region   ON cases(region);
CREATE INDEX IF NOT EXISTS idx_cases_category ON cases(category);
CREATE INDEX IF NOT EXISTS idx_cases_deadline ON cases(deadline);
-- procurement_type / budget_yen の索引は init_db() のマイグレーション後に作成
-- （既存DBでは列追加が先に必要なため）。

CREATE TABLE IF NOT EXISTS profile (
    id           INTEGER PRIMARY KEY CHECK (id = 1),  -- 単一行
    company      TEXT DEFAULT '',   -- 自社名（競合一覧から自社を除外する）
    prefectures  TEXT DEFAULT '',   -- 対応エリア（都道府県, カンマ区切り）
    categories   TEXT DEFAULT '電気工事',  -- 対応業種（カンマ区切り）
    budget_max   TEXT DEFAULT '',   -- 予算上限（予定価格がこれ以下）。空=制限なし
    grade        TEXT DEFAULT '',   -- 経審等級（A〜E, 参考）
    quals        TEXT DEFAULT '',   -- 保有資格メモ
    updated_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS applications (
    case_id      INTEGER PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT '検討中',  -- APP_STATUSES のいずれか
    applied_date TEXT DEFAULT '',                 -- 申請日（任意）
    note         TEXT DEFAULT '',                 -- メモ
    updated_at   TEXT DEFAULT (datetime('now'))
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
"""

# 入札参加申請のステータス（電気工事入札の進行段階）
APP_STATUSES: list[str] = [
    "検討中",        # これから検討
    "申請準備中",    # 参加資格・書類を準備
    "申請済",        # 入札参加申請を提出済み
    "入札参加済",    # 入札に参加した
    "落札",          # 落札できた
    "不参加",        # 参加しないと決定
    "見送り",        # 今回は見送り
]


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
        # cases の後付け列をマイグレーション（無ければ追加）
        case_cols = [r[1] for r in conn.execute("PRAGMA table_info(cases)")]
        if "description" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN description TEXT DEFAULT ''")
        if "procurement_type" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN procurement_type TEXT DEFAULT ''")
        if "budget_yen" not in case_cols:
            conn.execute("ALTER TABLE cases ADD COLUMN budget_yen INTEGER DEFAULT 0")
        # 列追加後に索引を作成（新設列のため SCHEMA からは外してある）
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_proctype ON cases(procurement_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cases_budgetyen ON cases(budget_yen)")
        conn.commit()


def upsert_cases(rows: list[dict[str, Any]]) -> int:
    """案件を一括投入。external_id が衝突したら上書き更新。投入件数を返す。"""
    cols = [
        "source", "external_id", "title", "agency", "agency_type",
        "region", "prefecture", "category", "procurement_type", "bid_method",
        "announced_date", "deadline", "detail_url", "spec_status", "spec_reason",
        "spec_url", "budget", "budget_yen", "winner", "win_price", "description",
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
        return r.get(c, "")

    with _connect() as conn:
        conn.executemany(sql, [tuple(_val(r, c) for c in cols) for r in rows])
        conn.commit()
    return len(rows)


def _as_list(v: Any) -> list[str]:
    """str / list / None を、空要素を除いた文字列リストに正規化する。"""
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    return [str(x).strip() for x in v if str(x).strip()]


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
      - q: 空白/カンマ区切りで複数キーワード可（いずれか一致＝OR）
    """
    where: list[str] = []
    params: list[Any] = []

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
    # キーワードは空白/カンマ区切りで複数可。各語が title/agency いずれかに一致（語間OR）
    terms = [t for t in q.replace("，", ",").replace("、", ",").replace(",", " ").split() if t]
    if terms:
        ors = " OR ".join("(title LIKE ? OR agency LIKE ?)" for _ in terms)
        where.append(f"({ors})")
        for t in terms:
            params.extend([f"%{t}%", f"%{t}%"])

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    # 締切が空文字の案件は末尾に回す
    order = {
        "deadline": "CASE WHEN deadline = '' THEN 1 ELSE 0 END, deadline ASC",
        "announced": "announced_date DESC",
        "budget": "budget_yen DESC",
    }.get(sort, "deadline ASC")

    sql = f"SELECT * FROM cases {clause} ORDER BY {order} LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_case(case_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        return dict(row) if row else None


def distinct_values(column: str) -> list[str]:
    """フィルタUIの候補値（指定カラムの非空ユニーク値）。"""
    allowed = {"category", "procurement_type", "bid_method", "prefecture",
               "region", "agency_type"}
    if column not in allowed:
        raise ValueError(f"許可されていないカラム: {column}")
    sql = f"SELECT DISTINCT {column} FROM cases WHERE {column} != '' ORDER BY {column}"
    with _connect() as conn:
        return [r[0] for r in conn.execute(sql).fetchall()]


def count_cases() -> int:
    with _connect() as conn:
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


def find_agency_for_case(agency_name: str) -> dict[str, Any] | None:
    """案件の発注機関名から agencies テーブルの機関情報を探す。

    官公需APIの形式 "大阪府吹田市" → agencies "吹田市役所" のようなマッチを行う。
    """
    import re
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

        # 4) 省庁名の先頭キーワードでマッチ
        keyword = agency_name.split("／")[0].split(" ")[0][:6]
        if len(keyword) >= 2:
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

def set_application(case_id: int, status: str, applied_date: str = "", note: str = "") -> None:
    """案件の入札参加申請ステータスを登録・更新する。"""
    if status not in APP_STATUSES:
        raise ValueError(f"不正なステータス: {status}")
    with _connect() as conn:
        conn.execute(
            """INSERT INTO applications (case_id, status, applied_date, note, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))
               ON CONFLICT(case_id) DO UPDATE SET
                 status=excluded.status, applied_date=excluded.applied_date,
                 note=excluded.note, updated_at=datetime('now')""",
            (case_id, status, applied_date, note),
        )
        conn.commit()


def get_application(case_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM applications WHERE case_id = ?", (case_id,)
        ).fetchone()
        return dict(row) if row else None


def list_applications(status: str | None = None) -> list[dict[str, Any]]:
    """申請管理一覧（案件情報をJOINして返す）。新しい更新順。"""
    sql = """
        SELECT a.*, c.title, c.agency, c.region, c.prefecture,
               c.deadline, c.spec_status
        FROM applications a
        JOIN cases c ON c.id = a.case_id
    """
    params: list[Any] = []
    if status:
        sql += " WHERE a.status = ?"
        params.append(status)
    sql += " ORDER BY a.updated_at DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


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


def get_profile() -> dict[str, Any]:
    """マイ条件を取得（未設定なら空の既定値）。"""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM profile WHERE id = 1").fetchone()
    if row:
        return dict(row)
    return {"id": 1, "company": "", "prefectures": "", "categories": "電気工事",
            "budget_max": "", "grade": "", "quals": ""}


def save_profile(prefectures: str, categories: str, budget_max: str,
                 grade: str = "", quals: str = "", company: str = "") -> None:
    """マイ条件を保存（単一行 upsert）。"""
    with _connect() as conn:
        conn.execute(
            """INSERT INTO profile (id, company, prefectures, categories, budget_max, grade, quals, updated_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, datetime('now'))
               ON CONFLICT(id) DO UPDATE SET
                 company=excluded.company, prefectures=excluded.prefectures,
                 categories=excluded.categories, budget_max=excluded.budget_max,
                 grade=excluded.grade, quals=excluded.quals, updated_at=datetime('now')""",
            (company, prefectures, categories, budget_max, grade, quals),
        )
        conn.commit()


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
