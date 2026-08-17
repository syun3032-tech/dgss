#!/bin/zsh
# 毎日の自動更新＋自動監査（launchd から呼ばれる・ランニングコスト0円）。
#
# 1) preflight.py     … 取得元に本当に繋がるか。駄目ならここで理由が残る
# 2) update.py --fast … 官公需API(工事+役務)＋監視機関をHTTPのみで高速取得（失敗時は既存データ維持）
#                       最後に「取得元別の件数」を検品し、重大なら非0で終わる
# 3) audit.py --pdf   … データ品質＆好機 ＋ ToDo精度を公告PDFと実照合(PDCAのCheck)
# 4) njss_org_daily.py … NJSS発注機関リストの続き取得（閲覧上限に達したら翌日続きから）
#
# 【多層防御・人に届く層】異常があってもログに書くだけだと誰も読まない。
# 2026-07-07から6週間、毎朝ちゃんと走っていたのに全滅に気づけなかったのがまさにそれ。
# なので重大な異常のときは macOS の通知センターに出す。ログは後追い用。
#
# 【2台のMacで同じものを使うためパスを固定しない】
# このファイル自身の場所からリポジトリを特定する。以前は絶対パス直書きで
# .gitignore されており、もう1台のMacには手で置く必要があった。
set -u
DIR="${0:A:h}"
LOG="$DIR/daily.log"
cd "$DIR" || exit 1

# venv が無い/壊れている場合に備えて python を選ぶ。
# homebrew python は pyexpat が壊れていることがあり（macOS 26.1で確認）、
# その場合 XML が一切parseできず取得が全滅するので、uv 管理のPythonで作り直す。
PY="$DIR/.venv/bin/python"
if ! "$PY" -c "import pyexpat" >/dev/null 2>&1; then
  echo "[$(date '+%F %T')] venvのpyexpatが壊れています。uvで作り直します。" >> "$LOG"
  if command -v uv >/dev/null 2>&1; then
    uv venv --python 3.13 "$DIR/.venv" >> "$LOG" 2>&1
    uv pip install --python "$PY" -r "$DIR/requirements-local.txt" >> "$LOG" 2>&1
  else
    echo "[$(date '+%F %T')] uv が無いため復旧できません。brew install uv を実行してください。" >> "$LOG"
  fi
fi

notify() {  # notify <タイトル> <本文>
  /usr/bin/osascript -e "display notification \"$2\" with title \"$1\" sound name \"Basso\"" 2>/dev/null
}

echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily start =====" >> "$LOG"

if ! "$PY" preflight.py >> "$LOG" 2>&1; then
  notify "入札データ取得エラー" "取得元に接続できません。daily.log を確認してください。"
fi

if ! "$PY" update.py --fast >> "$LOG" 2>&1; then
  notify "入札データが異常です" "検品で重大な指摘。データが痩せている可能性があります。"
fi

"$PY" audit.py --pdf 25 >> "$LOG" 2>&1
"$PY" njss_org_daily.py >> "$LOG" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M:%S') daily end =====" >> "$LOG"
