"""PDF実照合の品質監査（PDCAの中核・実行時AIなし）。

案件の公告PDFを取得・テキスト化し、ルールで「事実」を抽出する:
  - 物品調達か（"物品の製造…の競争入札参加資格" 等＝建設工事ではない）
  - 経営事項審査(経審)が要るか（"経営事項審査"/"総合評定値" の記載＝建設工事）
  - 必要な建設業許可の業種（電気工事業/管工事業/建築一式/とび・土工 …）
  - 紙入札か電子入札か

それを当方の application_requirements(case) の出力と突き合わせ、ToDo/必要書類が
公告の実態とズレていないかを検査する。新規案件を取り込んでも精度が崩れていないかを
日次で回すための監査（Plan-Do-Check-Act）に使う。

使い方:
  python audit_pdf.py --sample 40            # 開いている案件40件をPDF照合
  python audit_pdf.py --sample 40 --json out.json
  ※ pdftotext(poppler) が必要。無ければ pypdf にフォールバック。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import db
import procurement

# --- 公告本文から抽出する「事実」のシグナル -------------------------------------

# 建設工事を示す（経審が要る）
_KEISHIN_RE = re.compile(r"経営事項審査|総合評定値|総合数値\(P\)|経審")
# 物品調達/役務として発注（建設工事ではない）＝経審・建設業許可は不要。
# 注意: 「物品又は役務」は入札心得の定型文で建設工事の公告にも出るため使わない
# （誤検知の原因）。物品の"製造・購入"の参加資格を求める文言など、確度の高い signal のみ。
_BUPPIN_RE = re.compile(r"物品の製造[^。]{0,40}競争入札[^。]{0,20}参加|"
                        r"物品の製造、修理|物品の購入[^。]{0,30}参加資格|"
                        r"物品に係る競争入札参加資格|製造の請負[^。]{0,20}参加")
# 同時に「建設業(法)の許可」を要求していれば建設工事＝物品判定を打ち消す
_KENSETSU_LICENSE_RE = re.compile(r"建設業[のﾉ法]?[^。]{0,10}許可|建設業の許可")
# 必要な建設業許可の業種（PDFに明示されるもの）
_LICENSE_PATTERNS = [
    ("電気工事業", re.compile(r"電気工事業")),
    ("管工事業", re.compile(r"管工事業")),
    ("建築一式", re.compile(r"建築一式")),
    ("土木一式", re.compile(r"土木一式")),
    ("とび・土工", re.compile(r"とび・土工|とび土工")),
    ("塗装工事業", re.compile(r"塗装工事業")),
    ("防水工事業", re.compile(r"防水工事業")),
    ("造園工事業", re.compile(r"造園工事業")),
    ("解体工事業", re.compile(r"解体工事業")),
    ("舗装工事業", re.compile(r"ほ装工事業|舗装工事業")),
]
_PAPER_RE = re.compile(r"紙入札|紙による入札|入札書を持参|郵送による入札")
_ELEC_BID_RE = re.compile(r"電子入札|電子調達システム")


def extract_pdf_text(url: str, timeout: int = 25) -> str:
    """PDFをダウンロードしてテキスト化。失敗時は ""。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = res.read()
    except Exception:  # noqa: BLE001
        return ""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as f:
        f.write(data)
        f.flush()
        try:
            out = subprocess.run(["pdftotext", "-enc", "UTF-8", f.name, "-"],
                                 capture_output=True, timeout=30)
            if out.returncode == 0 and out.stdout:
                return out.stdout.decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            pass
        try:  # フォールバック: pypdf
            import pypdf
            r = pypdf.PdfReader(f.name)
            return "\n".join((p.extract_text() or "") for p in r.pages)
        except Exception:  # noqa: BLE001
            return ""


def ground_truth(pdf_text: str) -> dict:
    """PDF本文から「事実」を抽出する。"""
    t = pdf_text or ""
    licenses = [name for name, rx in _LICENSE_PATTERNS if rx.search(t)]
    # 建設業許可を要求していれば建設工事＝物品調達ではない（誤検知の打ち消し）
    is_buppin = bool(_BUPPIN_RE.search(t)) and not _KENSETSU_LICENSE_RE.search(t)
    return {
        "is_buppin": is_buppin,
        "has_keishin": bool(_KEISHIN_RE.search(t)),
        "licenses": licenses,
        "paper_bid": bool(_PAPER_RE.search(t)),
        "elec_bid": bool(_ELEC_BID_RE.search(t)),
        "chars": len(t),
    }


def _our_license(req: dict) -> str | None:
    """当方が必須書類で断定している建設業許可の業種名（"公告で確認"の時は None）。"""
    for d in req["documents"]:
        lab = d["label"]
        if "建設業許可" in lab and "公告で指定" not in lab:
            m = re.search(r"（([^）]+)）", lab)
            return m.group(1) if m else lab
    return None


def audit_case(case: dict, pdf_text: str) -> dict:
    """1案件: 当方の生成 vs PDFの事実 を突き合わせ、不一致(issues)を返す。"""
    req = procurement.application_requirements(case)
    kind = req["procurement_kind"]
    requires_keishin = any("経営事項審査" in d["label"] for d in req["documents"])
    our_lic = _our_license(req)
    gt = ground_truth(pdf_text)

    issues = []
    if gt["chars"] < 300:
        return {"skipped": "pdf_unreadable", "gt": gt}

    # 1) 経審の偽陽性: 当方は工事(経審必須)だが、公告は物品調達で経審の記載なし
    if requires_keishin and gt["is_buppin"] and not gt["has_keishin"]:
        issues.append(("経審_偽陽性",
                       "当方は工事として経審必須だが、公告は物品調達扱い（経審なし）"))
    # 2) 経審の偽陰性: 当方は役務(経審なし)だが、公告は経審を要求する建設工事
    if kind == "役務" and gt["has_keishin"] and not gt["is_buppin"]:
        issues.append(("経審_偽陰性",
                       "当方は役務で経審不要だが、公告に経営事項審査の記載あり（建設工事の可能性）"))
    # 3) 建設業許可の業種ズレ: 当方が断定した業種が、公告記載の業種と一致しない
    if our_lic and gt["licenses"]:
        # 当方の業種名(例 電気工事業/管工事業/造園工事業)が公告の業種群に含まれるか
        norm = our_lic.replace("証明書", "")
        if not any(name in our_lic or name in norm for name in gt["licenses"]):
            issues.append(("許可業種ズレ",
                           f"当方=「{our_lic}」 / 公告記載=「{', '.join(gt['licenses'])}」"))
    return {"issues": issues, "kind": kind, "our_license": our_lic, "gt": gt}


def run(sample: int = 40, only_open: bool = True) -> dict:
    """サンプル案件をPDF照合し、不一致を集計したレポートを返す。"""
    where = "detail_url LIKE '%.pdf' OR detail_url LIKE '%.PDF'"
    open_cond = " AND deadline >= date('now','localtime')" if only_open else ""
    with db._connect() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM cases WHERE source='官公需API' AND ({where}){open_cond} "
            f"ORDER BY RANDOM() LIMIT ?", (sample,)).fetchall()]

    texts = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for case, txt in zip(rows, ex.map(lambda c: extract_pdf_text(c["detail_url"]), rows)):
            texts[case["id"]] = txt

    checked = skipped = with_issues = 0
    by_type: dict[str, int] = {}
    details = []
    for case in rows:
        res = audit_case(case, texts.get(case["id"], ""))
        if res.get("skipped"):
            skipped += 1
            continue
        checked += 1
        if res["issues"]:
            with_issues += 1
            for typ, _ in res["issues"]:
                by_type[typ] = by_type.get(typ, 0) + 1
            details.append({
                "title": case["title"][:40], "kind": res["kind"],
                "issues": res["issues"], "licenses": res["gt"]["licenses"],
            })
    return {
        "sample": len(rows), "checked": checked, "skipped": skipped,
        "with_issues": with_issues,
        "accuracy": round(100 * (checked - with_issues) / checked, 1) if checked else 0,
        "by_type": by_type, "details": details,
    }


def _print_report(rep: dict) -> None:
    print(f"=== PDF実照合 品質監査 ===")
    print(f"サンプル {rep['sample']} / 照合可 {rep['checked']} / 読取不可 {rep['skipped']}")
    print(f"不一致のあった案件: {rep['with_issues']} / 整合率 {rep['accuracy']}%")
    print(f"不一致の内訳: {rep['by_type']}")
    for d in rep["details"][:30]:
        print(f"  ⚠ {d['title']} [{d['kind']}]")
        for typ, msg in d["issues"]:
            print(f"      - {typ}: {msg}")


if __name__ == "__main__":
    n = 40
    if "--sample" in sys.argv:
        n = int(sys.argv[sys.argv.index("--sample") + 1])
    report = run(sample=n, only_open="--all" not in sys.argv)
    _print_report(report)
    if "--json" in sys.argv:
        p = sys.argv[sys.argv.index("--json") + 1]
        Path(p).write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"\n→ JSON: {p}")
    # 整合率が低ければ非0終了（PDCAの警告フックに使える）
    sys.exit(0 if report["accuracy"] >= 90 or report["checked"] == 0 else 1)
