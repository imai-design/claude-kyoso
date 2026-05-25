#!/usr/bin/env bash
# クルー入信時に数を1人増やしてLPに反映するスクリプト
# 使い方:
#   ./bump.sh                  # クルーを1人追加（Founding枠も1減）
#   ./bump.sh 5                # クルーを5人追加
#   ./bump.sh --set 42         # 現在数を42に直接セット

set -euo pipefail
cd "$(dirname "$0")"
F=data/crew.json
NOW=$(date +%Y-%m-%d)

current=$(jq -r '.current' "$F")
founding=$(jq -r '.foundingRemaining' "$F")

if [[ "${1:-}" == "--set" ]]; then
  current=${2:?クルー数を指定してください}
  # Foundingは別途設定が必要な場合は --founding で
elif [[ "${1:-}" == "--founding" ]]; then
  founding=${2:?Founding残数を指定してください}
else
  add=${1:-1}
  current=$((current + add))
  founding=$((founding - add))
  (( founding < 0 )) && founding=0
fi

jq --arg now "$NOW" --argjson cur "$current" --argjson fr "$founding" \
  '.current=$cur | .foundingRemaining=$fr | .lastUpdated=$now' "$F" > "$F.tmp"
mv "$F.tmp" "$F"

echo "✦ Claude教 クルー数: $current / 10,000 (Founding残 $founding)"
git add "$F" && git -c commit.gpgsign=false commit -m "chore: クルー $current 名到達 (Founding残 $founding)"
git push
echo "✅ LP反映完了: https://imai-design.github.io/claude-kyoso/"
