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
import procurement
from regions import ALL_PREFECTURES, REGIONS, prefectures_in

# 公告日がこの日付以降なら「新着」とみなす（直近7日）
def _new_threshold() -> str:
    return (date.today() - timedelta(days=7)).isoformat()

# 予定価格の下限フィルタの選択肢（label, 円値）。本文から拾えた予定価格に対して効く。
BUDGET_OPTIONS = [
    ("指定なし", 0),
    ("500万円以上", 5_000_000),
    ("1000万円以上", 10_000_000),
    ("3000万円以上", 30_000_000),
    ("5000万円以上", 50_000_000),
    ("1億円以上", 100_000_000),
]

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


@app.context_processor
def inject_profile_set():
    """無料ホストはディスク揮発のためマイ条件が消える。ブラウザ保存→自動復元の判定用に、
    サーバにマイ条件があるかを全テンプレへ渡す。"""
    try:
        return {"profile_set": bool(db.get_profile().get("prefectures"))}
    except Exception:  # noqa: BLE001
        return {"profile_set": False}


@app.route("/")
def cases():
    # 初期表示は関西中心（クエリ無しのランディング時は近畿をデフォルト）
    if not request.args:
        region = "近畿"
    else:
        region = request.args.get("region", "").strip()
    prefecture = request.args.get("prefecture", "").strip()
    # 業種・区分・入札方式は複数選択（チェック/multiple select）に対応
    category = [c for c in request.args.getlist("category") if c.strip()]
    procurement_type = [p for p in request.args.getlist("procurement_type") if p.strip()]
    bid_method = [b for b in request.args.getlist("bid_method") if b.strip()]
    spec_status = request.args.get("spec_status", "").strip()
    q = request.args.get("q", "").strip()
    # 新着期間: ""=指定なし / today=本日公告 / week=直近7日。nav の new=1 は week 扱い。
    fresh = request.args.get("fresh", "").strip()
    if request.args.get("new") == "1" and not fresh:
        fresh = "week"
    new_only = fresh in ("today", "week")
    open_only = request.args.get("open") == "1"
    # 終了（締切が過去）案件を表示するか。既定は隠す（closed=1 で表示）。
    show_closed = request.args.get("closed") == "1"
    # 金額下限（円）。万円ではなく円で受ける（フォームの選択肢は円値）
    try:
        budget_min = int(request.args.get("budget_min", "") or 0)
    except ValueError:
        budget_min = 0
    sort = request.args.get("sort", "announced" if new_only else "deadline").strip()
    # ページング（1ページ200件）。?page=2 で次の200件。
    try:
        page = max(1, int(request.args.get("page", "1") or 1))
    except ValueError:
        page = 1
    per_page = 200

    # 地方が選ばれていて都道府県がその地方に属さない場合は都道府県条件を無視
    if region and prefecture and prefecture not in prefectures_in(region):
        prefecture = ""

    # 新着フィルタはSQL側で行う（後フィルタだと件数が不正確＆200件上限の影響を受けるため）
    threshold = _new_threshold()
    today_iso = date.today().isoformat()
    announced_after = today_iso if fresh == "today" else (threshold if fresh == "week" else None)

    filters = dict(
        region=region or None,
        prefecture=prefecture or None,
        category=category or None,
        procurement_type=procurement_type or None,
        bid_method=bid_method or None,
        spec_status=spec_status or None,
        budget_min=budget_min or None,
        open_only=open_only,
        hide_closed=not show_closed,
        q=q,
        announced_after=announced_after,
    )
    matched = db.count_list_cases(**filters)          # 該当件数（上限なしの実数）
    total_pages = max(1, (matched + per_page - 1) // per_page)
    page = min(page, total_pages)
    rows = db.list_cases(sort=sort, limit=per_page, offset=(page - 1) * per_page, **filters)

    # ページング表示用（1始まりの「N〜M件目」）
    pg = {
        "page": page, "per_page": per_page, "total_pages": total_pages,
        "matched": matched,
        "start": (0 if matched == 0 else (page - 1) * per_page + 1),
        "end": min(page * per_page, matched),
        "has_prev": page > 1, "has_next": page < total_pages,
    }
    # 終了タブ／ページャのリンク用に現在のクエリを保持（各々で上書きするキーは除く）
    base_args = {k: request.args.getlist(k) for k in request.args if k != "closed"}
    page_args = {k: request.args.getlist(k) for k in request.args if k != "page"}

    # 画面の文脈ヘッダー（どの絞り込みで見ているかを一目で分かるように）。
    # サイドバーのどの項目を選んでいるか（nav_active）も併せて決める。
    if budget_min > 0:
        man = f"{budget_min // 10000:,}"
        view = {
            "icon": "💡",
            "title": (f"今応募できる {man}万円以上の案件" if open_only
                      else f"{man}万円以上の案件"),
            "desc": "予定価格の高い順に表示中。" + (
                "締切が今日以降の電気工事案件にしぼっています。" if open_only
                else "予定価格が分かっている案件のみ対象です。"),
        }
        nav_active = "budget"
    elif fresh == "today":
        view = {"icon": "🆕", "title": "本日の新着案件",
                "desc": "今日公告された電気工事案件です。"}
        nav_active = "new"
    elif fresh == "week":
        view = {"icon": "🆕", "title": "新着案件（直近1週間）",
                "desc": "直近7日間に公告された電気工事案件です。"}
        nav_active = "new"
    else:
        view = {"icon": "🔎", "title": "案件を探す",
                "desc": "地方・都道府県・業種・予定価格などで絞り込めます。"}
        nav_active = "cases"

    return render_template(
        "cases.html",
        view=view,
        nav_active=nav_active,
        rows=rows,
        regions=REGIONS,
        # 選択中の地方に応じた都道府県候補（未選択なら全国）
        pref_options=prefectures_in(region) if region else [],
        categories=db.distinct_values("category"),
        procurement_types=db.distinct_values("procurement_type"),
        bid_methods=db.distinct_values("bid_method"),
        budget_options=BUDGET_OPTIONS,
        spec_reasons=db.SPEC_REASONS,
        total=db.count_cases(),
        new_threshold=threshold,
        today=date.today().isoformat(),
        show_closed=show_closed,
        base_args=base_args,
        page_args=page_args,
        pg=pg,
        selected={
            "region": region, "prefecture": prefecture, "category": category,
            "procurement_type": procurement_type, "bid_method": bid_method,
            "spec_status": spec_status, "budget_min": budget_min,
            "open": open_only, "q": q, "sort": sort, "new": new_only,
            "fresh": fresh,
        },
    )


@app.route("/case/<int:case_id>")
def case_detail(case_id: int):
    case = db.get_case(case_id)
    if not case:
        abort(404)
    agency_info = db.find_agency_for_case(case.get("agency", ""))
    # 応募導線（どこで申し込むか）は procurement.py が「確実なものだけ」を組み立てる。
    # スプレッドシートの当てにならない bid_url は使わない（誤誘導の原因だったため）。
    guide = procurement.application_guide(case, agency_info)
    # 必要書類・ToDo・応募内容を案件属性から確定的に導出（実行時AIなし）。
    requirements = procurement.application_requirements(case, guide)
    return render_template(
        "case_detail.html",
        c=case,
        spec_reasons=db.SPEC_REASONS,
        application=db.get_application(case_id),
        app_statuses=db.APP_STATUSES,
        status_class=STATUS_CLASS,
        agency_info=agency_info,
        guide=guide,
        requirements=requirements,
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


@app.route("/applications/restore", methods=["POST"])
def applications_restore():
    """ブラウザ(localStorage)に保存された申請をサーバDBへ復元する。

    サーバの denki_bid.db は毎日のデプロイで丸ごと差し替わり申請が消えるため、
    localStorage を真の保存先とし、ロード時にこのエンドポイントで静かに復元する。
    案件は external_id（再採番に強い安定キー）で現在の id に解決する。
    実際に新規・変更された件数だけ restored で返す（無駄な画面リロードの抑制用）。
    """
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    restored = 0
    for it in items:
        ext = (it.get("external_id") or "").strip()
        status = (it.get("status") or "").strip()
        if not ext or status not in db.APP_STATUSES:
            continue
        case_id = db.get_case_id_by_external(ext)
        if case_id is None:
            continue  # 現在のDBに該当案件が無い（公開終了等）→スキップ
        applied_date = (it.get("applied_date") or "").strip()
        note = (it.get("note") or "").strip()
        cur = db.get_application(case_id)
        if (cur and cur.get("status") == status
                and (cur.get("applied_date") or "") == applied_date
                and (cur.get("note") or "") == note):
            continue  # 既に同一内容ならスキップ
        db.set_application(case_id, status, applied_date, note)
        restored += 1
    return jsonify({"restored": restored})


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


# 社名に "/" が含まれても拾えるよう path コンバータを使う（通常の <name> だと
# スラッシュでルートが切れて 404 になるため）。
@app.route("/competitor/<path:name>")
def competitor_detail(name: str):
    """1社の落札実績一覧。"""
    cases = db.competitor_cases(name)
    if not cases:
        abort(404)
    return render_template("competitor_detail.html", name=name, cases=cases)


@app.route("/export.csv")
def export_csv():
    """強化済みDB（全案件）をCSVでダウンロード。"""
    from flask import Response
    csv_text = db.export_cases_csv()
    return Response(
        "﻿" + csv_text,  # BOM付きでExcel文字化け防止
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=kawano_njss_cases.csv"},
    )


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
