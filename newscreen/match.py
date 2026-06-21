"""新画面（AIモーニングマッチ）— 既存アプリから独立した Flask Blueprint。

ビジョン:
  毎朝の新着案件を「マイ条件」で絞って一覧表示。タップすると AI(Gemini) が
  「これはこういう案件・参加資格はこれ・あなたは持っているので応募できます・概要」
  をその場で判定し、『やりますか？』まで案内する。

設計（重要）:
  - 既存の app.py / templates を一切変更しない。新規ファイルだけで完結する。
  - 既存資産（db / ai_assist / procurement）は read-only で再利用する。
  - 後で本体に統合するときは app.py に2行足すだけ:
        from newscreen import match_bp
        app.register_blueprint(match_bp)
  - 単体開発は newscreen/standalone.py で別ポート(5002)で起動できる。

コスト方針:
  一覧は確定ロジック（AIなし＝0円）。AI判定は「タップした案件だけ」オンデマンドで
  1回呼ぶ（Geminiの無料枠／有料でも約0.1〜0.6円/タップ）。結果は ai_assist テーブルに
  キャッシュし、再タップでは無課金。
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

from flask import Blueprint, abort, jsonify, render_template, request

# 既存モジュールを read-only で再利用（パスを通す）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db          # noqa: E402
import ai_assist   # noqa: E402
import procurement  # noqa: E402

match_bp = Blueprint(
    "match", __name__,
    template_folder=str(Path(__file__).parent / "templates"),
)


def _profile_prefs() -> list[str]:
    """マイ条件の対応エリア（未設定なら空＝全国）。"""
    try:
        p = db.get_profile() or {}
        raw = p.get("prefectures") or ""
        return [x.strip() for x in raw.split(",") if x.strip()]
    except Exception:  # noqa: BLE001
        return []


def morning_matches(days: int = 14, limit: int = 60) -> list[dict]:
    """毎朝の新着マッチ（確定ロジック・AIなし）。

    新着(直近days日に公告) かつ 今応募できる(締切が今日以降) を、マイ条件の
    対応エリアと電気スコープで絞り、締切が近い順に返す。
    """
    today = date.today().isoformat()
    since = (date.today() - timedelta(days=days)).isoformat()
    prefs = _profile_prefs()

    where = [
        "announced_date >= ?",
        "deadline != '' AND deadline >= ?",
        # 電気スコープ（本業）＋役務（塗装/防水等も拾う方針）。IT/事務系のその他は除外。
        "(category LIKE '%電気工事%' OR procurement_type = '役務')",
        "category != 'その他'",
    ]
    params: list = [since, today]
    if prefs:
        where.append("prefecture IN (%s)" % ",".join("?" * len(prefs)))
        params.extend(prefs)
    sql = (
        "SELECT id, external_id, title, agency, agency_type, prefecture, region, "
        "category, procurement_type, bid_method, announced_date, deadline, "
        "budget, budget_yen, detail_url "
        f"FROM cases WHERE {' AND '.join(where)} "
        "ORDER BY deadline ASC LIMIT ?"
    )
    params.append(limit)
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    for r in rows:
        # 既にAI判定済みか（キャッシュの有無）をバッジ表示に使う
        cached = db.get_ai_assist(r["external_id"])
        r["ai_done"] = bool(cached)
        if cached:
            try:
                r["ai_verdict"] = json.loads(cached["payload"]).get(
                    "eligibility", {}).get("verdict", "")
            except Exception:  # noqa: BLE001
                r["ai_verdict"] = ""
    return rows


@match_bp.get("/match")
def match_list():
    """新画面: 毎朝のマッチ一覧。"""
    rows = morning_matches()
    prefs = _profile_prefs()
    return render_template("newscreen/match.html", cases=rows, prefs=prefs,
                           ai_enabled=ai_assist.is_enabled())


@match_bp.post("/match/<int:case_id>/brief")
def match_brief(case_id: int):
    """タップした案件をAIが判定して返す（オンデマンド・キャッシュ付き）。"""
    case = db.get_case(case_id)
    if not case:
        abort(404)
    ext = case.get("external_id", "")
    refresh = request.args.get("refresh") == "1"

    if not refresh:
        cached = db.get_ai_assist(ext)
        if cached:
            data = json.loads(cached["payload"])
            data["cached"] = True
            return jsonify(data)

    if not ai_assist.is_enabled():
        return jsonify({"enabled": False})

    try:
        req = procurement.application_requirements(case)
        result = ai_assist.assist(case, db.get_profile(), req)
    except Exception as e:  # noqa: BLE001
        return jsonify({"enabled": True, "error": str(e)[:200]}), 200

    if result.get("enabled") and ext:
        db.set_ai_assist(ext, json.dumps(result, ensure_ascii=False),
                         result.get("model", ""))
    result["cached"] = False
    return jsonify(result)
