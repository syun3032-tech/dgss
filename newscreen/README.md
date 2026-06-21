# 新画面：AIモーニングマッチ（別プロジェクト扱い）

毎朝の新着案件を「マイ条件」で絞って一覧表示し、**タップするとAI(Gemini)が
「これはこういう案件・参加資格はこれ・あなたは持っているので応募できます・概要」
を判定して「やりますか？」まで案内する**新しい画面。

## なぜ別ファイル/別ブランチか
本体（app.py / templates）を一切触らずに開発するため。いつでも安定版に戻せる。
- 安定チェックポイント: タグ `stable-2026-06-22`（`git reset --hard stable-2026-06-22` で完全復帰）
- 本画面の作業ブランチ: `feature/ai-morning-match`

## 単体で動かす（本体に影響なし・別ポート5002）
```bash
cd kawano-njss-modoki
.venv/bin/python newscreen/standalone.py
# → http://127.0.0.1:5002/match
```
DBは本体と同じ `denki_bid.db` を read-only 参照。AI判定は `.env` の `GEMINI_API_KEY` があれば有効。

## 構成（すべて新規ファイル・既存は不変）
- `newscreen/match.py` … Flask Blueprint（一覧=確定ロジック／AI判定=オンデマンド）
- `newscreen/templates/newscreen/match.html` … 画面
- `newscreen/standalone.py` … 単体起動
- 既存資産は read-only 再利用：`db` / `ai_assist` / `procurement`

## あとで本体に統合する（2行）
`app.py` に追記するだけ：
```python
from newscreen import match_bp
app.register_blueprint(match_bp)
```
これで本体に `/match` が生える。ナビに導線を足せば完成。

## コスト
- 一覧：確定ロジック（AIなし＝0円）
- AI判定：タップした案件だけ1回（Gemini無料枠／有料でも約0.1〜0.6円/タップ）。結果は `ai_assist` テーブルにキャッシュ＝再タップ無課金。

## まだ未実装（次の一手）
- 「進める」→ 申請管理(applications)へ自動登録（締切・提出方法・必要書類をセット）
- 毎朝の新着をプッシュ/メールで通知
- 一覧に出す前の事前AI判定（コスト管理のため既定はオンデマンド）
