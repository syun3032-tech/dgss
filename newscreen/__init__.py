"""新画面（AIモーニングマッチ）パッケージ。

本体への統合は app.py に2行:
    from newscreen import match_bp
    app.register_blueprint(match_bp)
"""
from .match import match_bp  # noqa: F401
