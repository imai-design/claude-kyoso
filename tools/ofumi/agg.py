#!/usr/bin/env python3
"""
御焚き上げCLI (ofumi) — ローカル集計器 [python3 stdlib / 3.9 互換]

~/.claude/projects/**/*.jsonl を走査し、Claude Code のトークン消費を集計する。
node/ccusage が無い環境でのフォールバック。出力は ccusage 互換の正規化JSON(stdout)。

お布施($)について: モデル別の正確な単価は ccusage に委ねる。python 経路は
トークン量のみを正とし totalCost は null を返す(粗い概算で番付を誤らせないため)。

プライバシー(最重要): このスクリプトは集計した「数値」だけを stdout に出す。
cwd / プロジェクト名 / 会話本文 / ファイルパス / sessionId は一切出力しない。
"""
import argparse
import glob
import json
import os
import sys
from datetime import datetime, timedelta


def num(x):
    """安全な数値化。bool は int 派生なので明示除外(True が 1 として混入するのを防ぐ)。"""
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else 0


def main():
    ap = argparse.ArgumentParser(description="ofumi local aggregator (Claude Code token usage)")
    ap.add_argument("--dir", default=os.environ.get(
        "OFUMI_PROJECTS_DIR", os.path.expanduser("~/.claude/projects")))
    args = ap.parse_args()

    files = glob.glob(os.path.join(args.dir, "**", "*.jsonl"), recursive=True)

    seen = set()
    tot = {"input": 0, "output": 0, "cc": 0, "cr": 0}
    by_model = {}
    asst = 0
    dups = 0

    # 期間境界はローカルTZで切る(UTCの[:10]切りは ccusage と隣接日でズレるため不可)
    now = datetime.now().astimezone()
    tz = now.tzinfo
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    month_total = 0
    week_total = 0

    for fp in files:
        try:
            fh = open(fp, "r", encoding="utf-8", errors="replace")
        except Exception:
            continue  # 開けないファイルのみスキップ
        with fh:
            for line in fh:
                # 1行ごとに保護。1行の異常で残り全行を捨てない(過少集計の防止)。
                try:
                    line = line.strip()
                    if not line:
                        continue
                    obj = json.loads(line)
                    if obj.get("type") != "assistant":
                        continue
                    msg = obj.get("message") or {}
                    u = msg.get("usage")
                    if not isinstance(u, dict):
                        continue
                    asst += 1

                    # dedup: message.id + requestId をファイル跨ぎでグローバル排除
                    mid = msg.get("id")
                    rid = obj.get("requestId")
                    if mid is not None:
                        key = (mid, rid)
                        if key in seen:
                            dups += 1
                            continue
                        seen.add(key)

                    i = num(u.get("input_tokens"))
                    o = num(u.get("output_tokens"))
                    cc = num(u.get("cache_creation_input_tokens"))
                    cr = num(u.get("cache_read_input_tokens"))
                    tot["input"] += i
                    tot["output"] += o
                    tot["cc"] += cc
                    tot["cr"] += cr
                    mt = i + o + cc + cr

                    model = msg.get("model", "unknown")
                    bm = by_model.setdefault(
                        model, {"input": 0, "output": 0, "cc": 0, "cr": 0, "calls": 0})
                    bm["input"] += i
                    bm["output"] += o
                    bm["cc"] += cc
                    bm["cr"] += cr
                    bm["calls"] += 1

                    ts = obj.get("timestamp")
                    if ts:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
                        if dt >= month_start:
                            month_total += mt
                        if dt >= week_start:
                            week_total += mt
                except Exception:
                    continue  # この行だけスキップ

    total = tot["input"] + tot["output"] + tot["cc"] + tot["cr"]
    out = {
        "totals": {
            "inputTokens": tot["input"],
            "outputTokens": tot["output"],
            "cacheCreationTokens": tot["cc"],
            "cacheReadTokens": tot["cr"],
            "totalTokens": total,
            "totalCost": None,  # python 経路は$を出さない(正確な額は ccusage 経路で)
        },
        "periods": {
            "month": {"totalTokens": month_total},
            "week": {"totalTokens": week_total},
        },
        # byModel はローカル表示専用。送信ペイロードには載せない(render.py が捨てる)。
        "byModel": {
            m: {"calls": v["calls"], "totalTokens": v["input"] + v["output"] + v["cc"] + v["cr"]}
            for m, v in by_model.items()
        },
        "meta": {
            "assistantMessages": asst,
            "dedupedRows": dups,
            "engine": "python",
            "tz": str(tz),
            "files": len(files),
        },
    }
    json.dump(out, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
