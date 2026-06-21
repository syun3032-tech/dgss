"""新画面を本体と切り離して単体起動する（別ポート5002）。

本体(app.py)を一切触らずに新画面だけを開発・確認するための入口。
    cd kawano-njss-modoki
    .venv/bin/python newscreen/standalone.py
    → http://127.0.0.1:5002/match

DBは本体と同じ denki_bid.db を read-only で参照する。AI判定は GEMINI_API_KEY
（.env）があれば有効。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, redirect  # noqa: E402

from newscreen import match_bp  # noqa: E402


def create_app() -> Flask:
    app = Flask(__name__)
    app.register_blueprint(match_bp)

    @app.get("/")
    def _root():
        return redirect("/match")

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5002, debug=True)
