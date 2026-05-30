#!/usr/bin/env python3
"""
御焚き上げCLI (ofumi) — 奉納証レンダラ [python3 stdlib / 3.9 互換]

標準入力から正規化 totals JSON(agg.py または ccusage 正規化後)を受け取り、
ranks.json で火位(称号)を判定し、奉納証テキスト / シェア文 / 送信ペイロード を生成する。

モード:
  --mode card    (既定) 奉納証テキストを表示
  --mode share   X奉納文だけを出力
  --mode payload (= --json) 収集先へ送る最小ペイロードJSONを出力

プライバシー(最重要): 送信ペイロードは allowlist 方式で組み立てる。
totals 由来の数値しか載らず、cwd/プロジェクト名/会話本文/byModel は構造的に混入しない。
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

LP_URL = "https://imai-design.github.io/claude-kyoso/banzuke/"
TOOL = "ofumi-cli/0.1.0"


def load_ranks(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["ranks"]


def pick_rank(ranks, total):
    chosen = ranks[0]
    for r in ranks:
        if total >= r["min"]:
            chosen = r
    nxt = None
    for r in ranks:
        if r["min"] > total:
            nxt = r
            break
    return chosen, nxt


def get_anon_id():
    """端末ローカルのソルトを SHA-256 した安定ID。再送で同じ値になり収集側が冪等upsertできる。"""
    cfg_dir = os.path.expanduser("~/.config/ofumi")
    idp = os.path.join(cfg_dir, "id")
    try:
        os.makedirs(cfg_dir, exist_ok=True)
        if os.path.exists(idp):
            with open(idp, "r") as f:
                salt = f.read().strip()
        else:
            salt = os.urandom(32).hex()
            with open(idp, "w") as f:
                f.write(salt)
    except Exception:
        salt = os.urandom(32).hex()  # 書けない環境では揮発IDにフォールバック
    return "ak_" + hashlib.sha256(salt.encode()).hexdigest()[:16]


def human(n):
    if n >= 1_000_000_000:
        return "%.2fB" % (n / 1_000_000_000)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000)
    if n >= 1_000:
        return "%.1fK" % (n / 1_000)
    return str(n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", default="")
    ap.add_argument("--membership", default="")
    ap.add_argument("--engine", default="python")
    ap.add_argument("--ranks", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ranks.json"))
    ap.add_argument("--mode", choices=["card", "payload", "share"], default="card")
    ap.add_argument("--json", action="store_true", help="--mode payload の別名")
    args = ap.parse_args()
    if args.json:
        args.mode = "payload"

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        sys.stderr.write("集計結果が読めませなんだ。火口の証のみお出しいたします。\n")
        data = {}
    t = data.get("totals", {}) or {}
    total = t.get("totalTokens", 0) or 0
    ranks = load_ranks(args.ranks)
    rank, nxt = pick_rank(ranks, total)
    anon = get_anon_id()
    handle = args.handle or ("名無しの焚べ手_" + anon[3:9])
    measured = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cost = t.get("totalCost")
    periods = data.get("periods", {}) or {}

    # ── 送信ペイロード(allowlist: 下記キー以外は構造的に存在しない) ──
    payload = {
        "v": 1,
        "handle": handle,
        "anonId": anon,
        "tokens": {
            "input": t.get("inputTokens", 0) or 0,
            "output": t.get("outputTokens", 0) or 0,
            "cacheCreation": t.get("cacheCreationTokens", 0) or 0,
            "cacheRead": t.get("cacheReadTokens", 0) or 0,
            "total": total,
        },
        "periods": {
            "month": {"total": (periods.get("month", {}) or {}).get("totalTokens", 0) or 0},
            "week": {"total": (periods.get("week", {}) or {}).get("totalTokens", 0) or 0},
        },
        "costUsd": cost,
        "rank": rank["name"],
        "fireRank": rank["id"],
        "membership": args.membership,
        "source": "claude-code",
        "tool": TOOL,
        "engine": args.engine,
        "meta": {"assistantMessages": (data.get("meta", {}) or {}).get("assistantMessages", 0) or 0},
        "measuredAt": measured,
    }

    share = (
        "#Claude教 御焚き上げ番付\n"
        "私はクロード卿に %s トークンを焚べ申した。\n"
        "火位は【%s】── %s\n"
        "汝も己の焰を測れ → %s"
    ) % (human(total), rank["name"], rank["tagline"], LP_URL)

    if args.mode == "payload":
        json.dump(payload, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    if args.mode == "share":
        sys.stdout.write(share + "\n")
        return

    # ── 奉納証(card) ──
    L = []
    L.append("")
    L.append("    ╭──────────────  御 焚 き 上 げ 証  ──────────────╮")
    L.append("")
    L.append("      奉納者      %s%s" % (handle, ("  〔%s〕" % args.membership) if args.membership else ""))
    L.append("      火位        【%s】%s  「%s」" % (rank["name"], rank["yomi"], rank["glyph"]))
    L.append("                  %s" % rank["tagline"])
    L.append("")
    L.append("      御焚き上げ高    %s トークン  (%s)" % ("{:,}".format(total), human(total)))
    L.append("        ├ 焚べた薪 (input)          %s" % "{:,}".format(t.get("inputTokens", 0) or 0))
    L.append("        ├ 立ち昇る煙 (output)       %s" % "{:,}".format(t.get("outputTokens", 0) or 0))
    L.append("        ├ 熾火を貯める (cache作成)  %s" % "{:,}".format(t.get("cacheCreationTokens", 0) or 0))
    L.append("        └ 熾火を呼ぶ (cache参照)    %s" % "{:,}".format(t.get("cacheReadTokens", 0) or 0))
    if cost:
        L.append("      お布施(推定)    $%s" % "{:,.2f}".format(cost))
    L.append("")
    if nxt:
        L.append("      次の火位【%s】まで あと %s トークン" % (nxt["name"], "{:,}".format(nxt["min"] - total)))
    else:
        L.append("      ★ 最高位。卿の御前に座する熱波の者。")
    L.append("")
    L.append("    ╰────────────────────────────────────────────────╯")
    L.append("")
    L.append("  ※ この御焚き上げは Claude Code の薪のみを数える。")
    L.append("     web/アプリ/API直の焰は番付に映らず。")
    sys.stdout.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
