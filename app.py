"""電気入札サーチ — 独立ツール（Flask）。

NJSS無双君 とは完全に独立したアプリ。SQLite だけで動く。

ルート:
  /            案件一覧（地方→都道府県の2段フィルタ、NJSS風）
  /case/<id>   案件詳細（仕様書の取得可否＋理由を表示）
  /api/prefectures  地方→都道府県の連動ドロップダウン用JSON

起動:
  cd denki-nyusatsu
  python app.py        → http://127.0.0.1:5001
"""

from __future__ import annotations

import os
from datetime import date, timedelta

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for

import db
from regions import ALL_PREFECTURES, REGIONS, prefectures_in

# 公告日がこの日付以降なら「新着」とみなす（直近30日）
def _new_threshold() -> str:
    return (date.today() - timedelta(days=30)).isoformat()

# 申請ステータスのバッジ色分け（テンプレートで使用）
STATUS_CLASS = {
    "検討中": "s-mull", "申請準備中": "s-prep", "申請済": "s-applied",
    "入札参加済": "s-joined", "落札": "s-won", "不参加": "s-no", "見送り": "s-skip",
}

# 対応業種の選択肢（電気工事業者向けに関連する建設業の業種）
BIZ_TYPES = [
    "電気工事", "電気設備工事", "電気通信工事", "機械器具設置工事",
    "管工事", "消防施設工事", "太陽光発電設備", "土木一式工事", "建築一式工事",
]

# 保有資格・登録の選択肢（複数選択）
QUAL_OPTIONS = [
    "建設業許可（電気工事業）", "第一種電気工事士", "第二種電気工事士",
    "電気主任技術者（電験）", "1級電気工事施工管理技士", "2級電気工事施工管理技士",
    "監理技術者", "経営事項審査（経審）", "入札参加資格登録",
    "ISO9001", "ISO14001", "Pマーク／ISMS",
]

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "kawano-njss-modoki-local")  # flash用

# gunicorn 等で import された時もテーブルを用意（本番デプロイ対応）
db.init_db()


@app.route("/")
def cases():
    # 初期表示は関西中心（クエリ無しのランディング時は近畿をデフォルト）
    if not request.args:
        region = "近畿"
    else:
        region = request.args.get("region", "").strip()
    prefecture = request.args.get("prefecture", "").strip()
    category = request.args.get("category", "").strip()
    bid_method = request.args.get("bid_method", "").strip()
    spec_status = request.args.get("spec_status", "").strip()
    q = request.args.get("q", "").strip()
    new_only = request.args.get("new") == "1"
    sort = request.args.get("sort", "announced" if new_only else "deadline").strip()

    # 地方が選ばれていて都道府県がその地方に属さない場合は都道府県条件を無視
    if region and prefecture and prefecture not in prefectures_in(region):
        prefecture = ""

    rows = db.list_cases(
        region=region or None,
        prefecture=prefecture or None,
        category=category or None,
        bid_method=bid_method or None,
        spec_status=spec_status or None,
        q=q,
        sort=sort,
    )
    threshold = _new_threshold()
    if new_only:
        rows = [r for r in rows if r.get("announced_date") and r["announced_date"] >= threshold]

    return render_template(
        "cases.html",
        rows=rows,
        regions=REGIONS,
        # 選択中の地方に応じた都道府県候補（未選択なら全国）
        pref_options=prefectures_in(region) if region else [],
        categories=db.distinct_values("category"),
        bid_methods=db.distinct_values("bid_method"),
        spec_reasons=db.SPEC_REASONS,
        total=db.count_cases(),
        new_threshold=threshold,
        selected={
            "region": region, "prefecture": prefecture, "category": category,
            "bid_method": bid_method, "spec_status": spec_status, "q": q, "sort": sort,
            "new": new_only,
        },
    )


@app.route("/case/<int:case_id>")
def case_detail(case_id: int):
    case = db.get_case(case_id)
    if not case:
        abort(404)
    return render_template(
        "case_detail.html",
        c=case,
        spec_reasons=db.SPEC_REASONS,
        application=db.get_application(case_id),
        app_statuses=db.APP_STATUSES,
        status_class=STATUS_CLASS,
    )


@app.route("/case/<int:case_id>/apply", methods=["POST"])
def apply_case(case_id: int):
    """案件の入札参加申請ステータスを登録・更新する。"""
    if not db.get_case(case_id):
        abort(404)
    status = request.form.get("status", "").strip()
    applied_date = request.form.get("applied_date", "").strip()
    note = request.form.get("note", "").strip()
    try:
        db.set_application(case_id, status, applied_date, note)
        flash(f"申請状況を「{status}」に更新しました。", "ok")
    except ValueError:
        flash("ステータスが不正です。", "error")
    return redirect(url_for("case_detail", case_id=case_id))


@app.route("/applications")
def applications():
    """入札参加申請の管理一覧。"""
    status = request.args.get("status", "").strip()
    return render_template(
        "applications.html",
        rows=db.list_applications(status or None),
        statuses=db.APP_STATUSES,
        status_class=STATUS_CLASS,
        selected_status=status,
    )


@app.route("/profile", methods=["GET", "POST"])
def profile():
    """マイ条件（対応エリア・業種・予算上限・保有資格）の設定。"""
    if request.method == "POST":
        prefectures = ",".join(request.form.getlist("prefectures"))
        # 業種・保有資格は複数選択。チェックに加え自由記入も結合する。
        categories = request.form.getlist("categories")
        cat_other = request.form.get("categories_other", "").strip()
        if cat_other:
            categories += [c.strip() for c in cat_other.split(",") if c.strip()]
        quals = request.form.getlist("quals")
        qual_other = request.form.get("quals_other", "").strip()
        if qual_other:
            quals += [q.strip() for q in qual_other.split(",") if q.strip()]
        budget_max = request.form.get("budget_max", "").strip()
        grade = request.form.get("grade", "").strip()
        company = request.form.get("company", "").strip()
        db.save_profile(prefectures, ",".join(categories) or "電気工事",
                        budget_max, grade, ",".join(quals), company=company)
        flash("マイ条件を保存しました。マッチ案件・競合企業に反映されます。", "ok")
        return redirect(url_for("matches"))

    prof = db.get_profile()
    return render_template(
        "profile.html",
        prof=prof,
        selected_prefs=[p for p in prof["prefectures"].split(",") if p],
        selected_cats=[c for c in prof["categories"].split(",") if c],
        selected_quals=[q for q in prof["quals"].split(",") if q],
        biz_types=BIZ_TYPES,
        qual_options=QUAL_OPTIONS,
        regions=REGIONS,
        grades=["", "A", "B", "C", "D", "E"],
    )


@app.route("/matches")
def matches():
    """マイ条件に合致する案件を、マッチ理由つきで表示。"""
    prof = db.get_profile()
    rows = db.match_cases(prof)
    return render_template(
        "matches.html",
        rows=rows,
        prof=prof,
        has_profile=bool(prof.get("prefectures")),
        spec_reasons=db.SPEC_REASONS,
        new_threshold=_new_threshold(),
    )


@app.route("/competitors")
def competitors():
    """自社の競合企業（落札者）の一覧。

    既定では「マイ条件の対応エリア」に絞り、「自社名」を除外して、
    “このシステムを使う会社（自社）の競合になりうる企業”だけを表示する。
    全国を見たい場合は ?all=1。
    """
    prof = db.get_profile()
    q = request.args.get("q", "").strip()
    prefecture = request.args.get("prefecture", "").strip()
    show_all = request.args.get("all") == "1"

    my_prefs = [p for p in (prof.get("prefectures") or "").split(",") if p]
    # 自社の対応エリアで絞る（all=1 か 手動で都道府県指定した時は除く）
    area = None if (show_all or prefecture) else (my_prefs or None)

    rows = db.list_competitors(
        q=q, prefecture=prefecture,
        prefectures=area,
        exclude_company=prof.get("company", ""),
    )
    return render_template(
        "competitors.html",
        rows=rows,
        prefectures=db.distinct_values("prefecture"),
        selected={"q": q, "prefecture": prefecture},
        my_company=prof.get("company", ""),
        my_area=my_prefs,
        scoped=bool(area),
        show_all=show_all,
    )


@app.route("/competitor/<name>")
def competitor_detail(name: str):
    """1社の落札実績一覧。"""
    cases = db.competitor_cases(name)
    if not cases:
        abort(404)
    return render_template("competitor_detail.html", name=name, cases=cases)


@app.route("/agencies")
def agencies():
    """監視対象の発注機関（全国）一覧。公式入札ページへの導線つき。"""
    import agency_import
    q = request.args.get("q", "").strip()
    rows = db.list_agencies(q=q)
    for r in rows:
        r["platform"] = agency_import.platform_of(r.get("domain", ""))
    return render_template("agencies.html", rows=rows, q=q, total=db.count_agencies())


@app.route("/api/prefectures")
def api_prefectures():
    """地方→都道府県の連動ドロップダウン用。"""
    region = request.args.get("region", "").strip()
    return jsonify(prefectures_in(region))


@app.template_filter("spec_label")
def spec_label(status: str) -> str:
    return {
        db.SPEC_AVAILABLE: "取得可",
        db.SPEC_UNAVAILABLE: "取得不可",
        db.SPEC_UNKNOWN: "未判定",
    }.get(status, "未判定")


if __name__ == "__main__":
    db.init_db()
    if db.count_cases() == 0:
        import seed_data
        n = seed_data.seed()
        print(f"DBが空だったのでサンプル {n} 件を投入しました。")
    # 環境変数で上書き可（デプロイ時は PORT/HOST が渡る）
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5001"))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host=host, port=port, debug=debug)
