#!/usr/bin/env bash
# ───────────────────────────────────────────────────────────────
#  御焚き上げCLI (ofumi) — Claude教 トークン奉納ランチャ
#
#  使い方:
#    curl -fsSL https://imai-design.github.io/claude-kyoso/tools/ofumi/ofumi.sh | bash
#    curl -fsSL .../ofumi.sh | bash -s -- --handle "あなたの名前" --submit
#    bash ofumi.sh --handle NAME [--submit] [--engine auto|ccusage|python] [--json] [--no-clipboard]
#
#  node/npx があれば ccusage を、無ければ同梱の python3 集計器を使う。
#  送信ペイロードには集計値とハンドルのみ載る(cwd/会話本文は送らない)。
# ───────────────────────────────────────────────────────────────
set -euo pipefail

HANDLE=""
MEMBERSHIP=""
SUBMIT=0
ENGINE="auto"
JSON_ONLY=0
NO_CLIP=0

while [ $# -gt 0 ]; do
  case "$1" in
    --handle) HANDLE="${2:-}"; shift 2;;
    --membership) MEMBERSHIP="${2:-}"; shift 2;;
    --submit) SUBMIT=1; shift;;
    --engine) ENGINE="${2:-auto}"; shift 2;;
    --json) JSON_ONLY=1; shift;;
    --no-clipboard) NO_CLIP=1; shift;;
    -h|--help)
      echo "ofumi.sh [--handle NAME] [--membership 学徒|信徒|神] [--submit] [--engine auto|ccusage|python] [--json] [--no-clipboard]"
      exit 0;;
    *) echo "未知のオプション: $1" >&2; shift;;
  esac
done

BASE_URL="${OFUMI_BASE_URL:-https://imai-design.github.io/claude-kyoso/tools/ofumi}"
ENDPOINT="${OFUMI_ENDPOINT:-}"
CLAUDE_DIR="${OFUMI_PROJECTS_DIR:-$HOME/.claude/projects}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 が見当たりませぬ。御焚き上げには python3 が要ります。" >&2
  exit 1
fi

if [ ! -d "$CLAUDE_DIR" ]; then
  echo "薪が見当たりませぬ: $CLAUDE_DIR が無い。" >&2
  echo "この端末で Claude Code を使った記録がありません。" >&2
  exit 0
fi

# スクリプト群の在処を決める(手元にあれば使い、curl|bash なら GitHub Pages から取得)
SELF_DIR=""
if [ -n "${BASH_SOURCE:-}" ] && [ -f "${BASH_SOURCE:-}" ]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE}")" && pwd)"
fi
if [ -n "$SELF_DIR" ] && [ -f "$SELF_DIR/agg.py" ] && [ -f "$SELF_DIR/render.py" ] && [ -f "$SELF_DIR/ranks.json" ]; then
  WORK="$SELF_DIR"
  CLEANUP=0
else
  WORK="$(mktemp -d)"
  CLEANUP=1
  for f in agg.py render.py ranks.json; do
    curl -fsSL "$BASE_URL/$f" -o "$WORK/$f" || { echo "取得失敗: $BASE_URL/$f" >&2; exit 1; }
  done
fi
cleanup() { [ "${CLEANUP:-0}" = "1" ] && rm -rf "$WORK" 2>/dev/null || true; }
trap cleanup EXIT

# engine 解決
resolve_engine() {
  case "$ENGINE" in
    ccusage) echo "ccusage"; return;;
    python)  echo "python";  return;;
  esac
  if command -v npx >/dev/null 2>&1; then echo "ccusage"; else echo "python"; fi
}
ENGINE_RESOLVED="$(resolve_engine)"

# ccusage --json の totals/daily を agg.py 互換スキーマへ正規化
# (daily 配列の period 日付から今週/今月をローカルTZ基準で算出)
normalize_ccusage() {
  # ccusage の RAW を環境変数で渡す($(cat)でstdin吸収)。
  # heredoc(python3 - <<EOF)は stdin をスクリプト本体に使うため、パイプのJSONと競合する。
  OFUMI_CCUSAGE_RAW="$(cat)" python3 - <<'PYEOF'
import sys, json, os
from datetime import date, timedelta
try:
    d = json.loads(os.environ.get("OFUMI_CCUSAGE_RAW", ""))
except Exception:
    sys.exit(2)
t = d.get("totals", {}) or {}
today = date.today()
monday = today - timedelta(days=today.weekday())
week_total = 0
month_total = 0
for day in (d.get("daily") or []):
    p = day.get("period") or day.get("date") or ""
    try:
        y, m, dd = (int(x) for x in p.split("-")[:3])
        dt = date(y, m, dd)
    except Exception:
        continue
    tt = day.get("totalTokens")
    if not isinstance(tt, (int, float)):
        tt = ((day.get("inputTokens", 0) or 0) + (day.get("outputTokens", 0) or 0)
              + (day.get("cacheCreationTokens", 0) or 0) + (day.get("cacheReadTokens", 0) or 0))
    if (dt.year, dt.month) == (today.year, today.month):
        month_total += tt
    if dt >= monday:
        week_total += tt
out = {
  "totals": {
    "inputTokens": t.get("inputTokens", 0) or 0,
    "outputTokens": t.get("outputTokens", 0) or 0,
    "cacheCreationTokens": t.get("cacheCreationTokens", 0) or 0,
    "cacheReadTokens": t.get("cacheReadTokens", 0) or 0,
    "totalTokens": t.get("totalTokens", 0) or 0,
    "totalCost": round(t.get("totalCost", 0) or 0, 2),
  },
  "periods": {"month": {"totalTokens": int(month_total)}, "week": {"totalTokens": int(week_total)}},
  "meta": {"assistantMessages": 0, "engine": "ccusage"},
}
json.dump(out, sys.stdout, ensure_ascii=False)
PYEOF
}

echo "卿の焰に薪をくべております…(engine: $ENGINE_RESOLVED)" >&2

TOTALS=""
if [ "$ENGINE_RESOLVED" = "ccusage" ]; then
  if RAW="$(npx -y ccusage@latest --json 2>/dev/null)" && [ -n "$RAW" ]; then
    if TOTALS="$(printf '%s' "$RAW" | normalize_ccusage)"; then
      :
    else
      echo "ccusage の解析に失敗。python 集計に切替えます。" >&2
      ENGINE_RESOLVED="python"
    fi
  else
    echo "ccusage の起動に失敗。python 集計に切替えます。" >&2
    ENGINE_RESOLVED="python"
  fi
fi
if [ "$ENGINE_RESOLVED" = "python" ]; then
  TOTALS="$(python3 "$WORK/agg.py" --dir "$CLAUDE_DIR")" || TOTALS=""
fi
if [ -z "$TOTALS" ]; then
  echo "集計に失敗いたしました。薪の記録が読めませなんだ。" >&2
  exit 1
fi

RENDER=(python3 "$WORK/render.py" --handle "$HANDLE" --membership "$MEMBERSHIP" --engine "$ENGINE_RESOLVED" --ranks "$WORK/ranks.json")

# --json だけ欲しい場合
if [ "$JSON_ONLY" = "1" ]; then
  printf '%s' "$TOTALS" | "${RENDER[@]}" --json
  exit 0
fi

# 奉納証を表示
printf '%s' "$TOTALS" | "${RENDER[@]}" --mode card

SHARE="$(printf '%s' "$TOTALS" | "${RENDER[@]}" --mode share)"
PAYLOAD="$(printf '%s' "$TOTALS" | "${RENDER[@]}" --json)"

# クリップボード(シェア文)
if [ "$NO_CLIP" = "0" ]; then
  if   command -v pbcopy  >/dev/null 2>&1; then printf '%s' "$SHARE" | pbcopy  && echo "📋 X奉納文をクリップボードに収めました。" >&2
  elif command -v wl-copy >/dev/null 2>&1; then printf '%s' "$SHARE" | wl-copy && echo "📋 クリップボードにコピー(wl-copy)。" >&2
  elif command -v xclip   >/dev/null 2>&1; then printf '%s' "$SHARE" | xclip -selection clipboard && echo "📋 クリップボードにコピー(xclip)。" >&2
  elif command -v clip    >/dev/null 2>&1; then printf '%s' "$SHARE" | clip    && echo "📋 クリップボードにコピー(clip)。" >&2
  fi
fi

echo "" >&2
echo "── X奉納文 ──" >&2
printf '%s\n' "$SHARE"

# 送信(--submit)
if [ "$SUBMIT" = "1" ]; then
  echo "" >&2
  if [ -z "$ENDPOINT" ]; then
    echo "⚠ 収集サーバー(OFUMI_ENDPOINT)未設定。奉納は GitHub Issue で受け付けます:" >&2
    echo "   https://github.com/imai-design/claude-kyoso/issues/new?labels=offering&template=offering.yml" >&2
    echo "   下記JSONを丸ごと貼って起票してください →" >&2
    echo "" >&2
    printf '%s\n' "$PAYLOAD"
  else
    if curl -fsS -X POST "$ENDPOINT/offer" -H 'content-type: application/json' --data "$PAYLOAD" >/dev/null 2>&1; then
      echo "🔥 番付へ奉納いたしました。" >&2
    else
      echo "送信に失敗。シェア文はクリップボードに残っています。" >&2
    fi
  fi
fi
