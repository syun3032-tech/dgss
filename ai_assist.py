"""AI応募アシスト（課金プラン向け・オンデマンド）。

設計方針（重要）:
  無料プランは AI を一切呼ばない＝ランニングコスト0。ユーザーが案件詳細で
  「AIで応募準備」をタップしたときだけ Claude API を1回呼び、公告本文・必要書類・
  マイ条件（保有資格/エリア/等級）を読み込んで『応募一歩手前』までの要点を生成する。
  結果は DB にキャッシュするので、同じ案件を再タップしても課金は発生しない。

有効化:
  環境変数 ANTHROPIC_API_KEY を設定する（Render / GitHub Actions の secret）。
  未設定なら機能は休眠（ボタンは出るが、押すと有効化方法を案内するだけ）。

モデル:
  既定 claude-opus-4-8（最も賢い）。AI_ASSIST_MODEL で上書き可（コスト調整用に
  claude-haiku-4-5 等へ変更できる）。
"""

from __future__ import annotations

import os
from typing import Any

import procurement

MODEL = os.environ.get("AI_ASSIST_MODEL", "claude-opus-4-8")

# Claude に返させる構造（構造化出力で型を保証＝実行時パース不要・壊れない）
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "array",
            "items": {"type": "string"},
            "description": "この案件の要点を3行で（何を・どこが発注・締切や金額の要点）",
        },
        "eligibility": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["〇", "△", "✕", "不明"]},
                "reasons": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["verdict", "reasons"],
            "additionalProperties": False,
            "description": "自社（マイ条件）がこの案件に参加できそうか。判定根拠も。",
        },
        "documents": {
            "type": "array",
            "items": {"type": "string"},
            "description": "この案件で実際に要りそうな提出書類を具体化（一般論でなく案件に即して）",
        },
        "todo": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["title", "detail"],
                "additionalProperties": False,
            },
            "description": "応募一歩手前までにやるべきことを順番に。最後は『入札書を出す直前』まで。",
        },
        "cautions": {
            "type": "array",
            "items": {"type": "string"},
            "description": "見落としやすい注意点（締切・資格要件・窓口受領のみ 等）",
        },
    },
    "required": ["summary", "eligibility", "documents", "todo", "cautions"],
    "additionalProperties": False,
}

_SYSTEM = (
    "あなたは日本の公共入札（電気工事系）に精通した入札支援の専門家です。"
    "与えられた案件の公告本文・確定的に算出済みの必要書類・ユーザーの保有資格(マイ条件)を"
    "読み込み、この事業者がこの案件に『応募する一歩手前』まで到達できるように具体的に支援します。"
    "一般論ではなく、この案件の実態に即して書くこと。"
    "参加資格適合の判定は、確証が無ければ△または不明とし、断定しすぎないこと。"
    "必要書類は発注機関により異なるため、最終確認は公告に当たるよう注意書きを添えること。"
)


def is_enabled() -> bool:
    """AI機能が有効か（APIキーが設定されているか）。"""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _profile_lines(profile: dict | None) -> str:
    p = profile or {}
    parts = []
    if p.get("company"):
        parts.append(f"自社名: {p['company']}")
    if p.get("prefectures"):
        parts.append(f"対応エリア(都道府県): {p['prefectures']}")
    if p.get("categories"):
        parts.append(f"対応業種: {p['categories']}")
    if p.get("grade"):
        parts.append(f"経審等級: {p['grade']}")
    if p.get("quals"):
        parts.append(f"保有資格: {p['quals']}")
    if p.get("budget_max"):
        parts.append(f"予算上限の目安: {p['budget_max']}")
    return "\n".join(parts) if parts else "（マイ条件は未設定）"


def _requirements_lines(req: dict | None) -> str:
    if not req:
        return "（必要書類の確定情報なし）"
    docs = req.get("documents") or []
    req_docs = [d["label"] for d in docs if d.get("required")]
    opt_docs = [d["label"] for d in docs if not d.get("required")]
    lines = [f"区分: {req.get('procurement_kind', '不明')}"]
    if req_docs:
        lines.append("必須(確定): " + " / ".join(req_docs))
    if opt_docs:
        lines.append("任意(確定): " + " / ".join(opt_docs))
    return "\n".join(lines)


def _build_user_text(case: dict, profile: dict | None, req: dict | None) -> str:
    desc = (case.get("description") or "").strip()
    return (
        "# 案件\n"
        f"案件名: {case.get('title', '')}\n"
        f"発注機関: {case.get('agency', '')}（{case.get('agency_type', '')}）\n"
        f"都道府県: {case.get('prefecture', '')} / 地方: {case.get('region', '')}\n"
        f"業種: {case.get('category', '')}\n"
        f"入札方式: {case.get('bid_method', '') or '不明'}\n"
        f"公告日: {case.get('announced_date', '') or '不明'} / 申込締切: {case.get('deadline', '') or '不明'}\n"
        f"予定価格: {case.get('budget', '') or '非公表/不明'}\n\n"
        "# 公告本文（抜粋）\n"
        f"{desc or '（本文なし。公告ページで要確認）'}\n\n"
        "# 確定的に算出済みの必要書類（土台。AIはこれを案件に即して具体化・補強する）\n"
        f"{_requirements_lines(req)}\n\n"
        "# 自社（マイ条件）\n"
        f"{_profile_lines(profile)}\n"
    )


def assist(case: dict, profile: dict | None = None,
           requirements: dict | None = None) -> dict[str, Any]:
    """案件1件に対しオンデマンドで AI 応募アシストを生成して返す。

    返り値: {"enabled": bool, "model": str, ...スキーマの各キー}。
    APIキー未設定なら {"enabled": False} を返す（呼び出し側で案内表示）。
    """
    if not is_enabled():
        return {"enabled": False}

    if requirements is None:
        try:
            requirements = procurement.application_requirements(case)
        except Exception:  # noqa: BLE001 — 土台が無くてもAIは動かす
            requirements = None

    import anthropic  # 遅延 import（未インストール環境でもアプリは起動する）

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _build_user_text(case, profile, requirements)}],
        output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
    )
    import json
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    data = json.loads(text)
    data["enabled"] = True
    data["model"] = resp.model
    return data
