#!/usr/bin/env python3
"""
collect.py — 御焚き上げ奉納Issueを集約し data/banzuke.json を生成 [python3 stdlib]

サーバー無し運用(Tier0)の集約器。gh CLI で offering ラベルのIssueを全取得し、
本文の JSON ペイロードを抽出・検証して番付データを再生成する。

使い方:
  python3 tools/ofumi/collect.py            # data/banzuke.json を再生成(dry: pushしない)
  python3 tools/ofumi/collect.py --push     # 再生成して git commit && push

依存: gh (認証済み), git。
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # claude-kyoso/
RANKS_PATH = os.path.join(ROOT, "tools", "ofumi", "ranks.json")
BANZUKE_PATH = os.path.join(ROOT, "data", "banzuke.json")
REPO = "imai-design/claude-kyoso"
IMPOSSIBLE = 100_000_000_000  # 1000億トークン超は捏造とみなし番付から除外

DISCLAIMER = ("本番付は各信徒が自己申告した Claude Code の消費トークンに基づく推定値です。"
              "claude.ai／API 直叩き分は含まれません。水増しの完全な防止はできません。"
              "番付は名誉と遊びであり、賞金や階位昇格などの実利とは切り離しています。")


def load_ranks():
    with open(RANKS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["ranks"]


def fire_rank(ranks, total):
    chosen = ranks[0]["id"]
    for r in ranks:
        if total >= r["min"]:
            chosen = r["id"]
    return chosen


def extract_json(body):
    if not body:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", body, re.S)
    raw = m.group(1) if m else None
    if not raw:
        s, e = body.find("{"), body.rfind("}")
        if s != -1 and e != -1 and e > s:
            raw = body[s:e + 1]
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


RESERVED_HANDLES = {"両聖ちゃん"}  # 教祖名。第三者の詐称を弾く(教祖は seed で保持される)
_HANDLE_RE = re.compile(r"\A[\w 　_・ー〜!?！？.\-]{2,24}\Z", re.UNICODE)


def valid_handle(h):
    return isinstance(h, str) and bool(_HANDLE_RE.match(h.strip()))


def normalize(payload, author, ranks):
    """Issueペイロードを検証し、内部レコードへ allowlist で詰め替える(未知キーは捨てる)。"""
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    h = payload.get("handle")
    if not valid_handle(h):
        return None
    if h.strip() in RESERVED_HANDLES:
        return None  # 予約名(教祖)の詐称を入口で遮断
    tk = payload.get("tokens") or {}
    try:
        i = int(tk.get("input", 0)); o = int(tk.get("output", 0))
        cc = int(tk.get("cacheCreation", 0)); cr = int(tk.get("cacheRead", 0))
        total = int(tk.get("total", 0))
    except Exception:
        return None
    if min(i, o, cc, cr, total) < 0:
        return None
    s = i + o + cc + cr
    if s > 0 and abs(total - s) > max(1, s * 0.001):
        total = s  # 内訳和と不一致なら和を正とする
    flags = []
    if total > IMPOSSIBLE:
        flags.append("impossible")
    week = ((payload.get("periods") or {}).get("week") or {}).get("total", 0) or 0
    cost = payload.get("costUsd")
    mem = payload.get("membership", "")
    return {
        "handle": h.strip(),
        "anonId": payload.get("anonId") if isinstance(payload.get("anonId"), str) else None,
        "membership": mem if mem in ("学徒", "信徒", "神") else "",
        "fireRank": fire_rank(ranks, total),
        "tokens": {"input": i, "output": o, "cacheCreation": cc, "cacheRead": cr, "total": total},
        "weekTotal": int(week) if isinstance(week, (int, float)) else 0,
        "estCostUsd": round(float(cost), 2) if isinstance(cost, (int, float)) and cost else None,
        "lastOfferingAt": payload.get("measuredAt") or "",
        "flags": flags,
        "author": author,
    }


def fetch_issues():
    cmd = ["gh", "issue", "list", "--repo", REPO, "--label", "offering",
           "--state", "open", "--json", "number,body,author", "--limit", "500"]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        print("gh issue list 失敗:", out.stderr, file=sys.stderr)
        return []
    try:
        return json.loads(out.stdout or "[]")
    except Exception:
        return []


def load_seeds():
    """既存 banzuke.json の isFounder エントリ(教祖など)を保持する。"""
    total_seed, week_seed = {}, {}
    if os.path.exists(BANZUKE_PATH):
        try:
            old = json.load(open(BANZUKE_PATH, encoding="utf-8"))
            for e in (old.get("periods", {}).get("total") or []):
                if e.get("isFounder"):
                    total_seed["h:" + e["handle"]] = e
            for e in (old.get("periods", {}).get("weekly") or []):
                if e.get("isFounder"):
                    week_seed["h:" + e["handle"]] = e
        except Exception:
            pass
    return total_seed, week_seed


def to_total_entry(rec):
    return {
        "handle": rec["handle"], "membership": rec["membership"], "fireRank": rec["fireRank"],
        "tokens": rec["tokens"], "estCostUsd": rec["estCostUsd"],
        "lastOfferingAt": rec["lastOfferingAt"], "flags": rec["flags"],
    }


def build(ranks):
    recs = {}
    for it in fetch_issues():
        author = (it.get("author") or {}).get("login", "")
        rec = normalize(extract_json(it.get("body", "")), author, ranks)
        if not rec:
            continue
        key = rec["anonId"] or ("h:" + rec["handle"])
        prev = recs.get(key)
        if prev is None or (rec["lastOfferingAt"] or "") >= (prev["lastOfferingAt"] or ""):
            recs[key] = rec

    total_seed, week_seed = load_seeds()

    def mkey(rec):
        # 集約キーは anonId 優先(同名別人を取り違えない)。seed は handle キー。
        return rec["anonId"] or ("h:" + rec["handle"])

    # ── total ── anonId優先で集約 → isFounder seed を最後に復元して保護
    total_map = {}
    for rec in recs.values():
        total_map[mkey(rec)] = to_total_entry(rec)
    for k, e in total_seed.items():
        total_map[k] = e  # 教祖(isFounder)を最優先で復元(同名上書きから保護)
    total = [e for e in total_map.values() if "impossible" not in (e.get("flags") or [])]
    total.sort(key=lambda e: e["tokens"]["total"], reverse=True)
    for i, e in enumerate(total):
        e["rank"] = i + 1

    # ── weekly ──
    week_map = {}
    for rec in recs.values():
        if rec["weekTotal"] > 0:
            week_map[mkey(rec)] = {
                "handle": rec["handle"], "membership": rec["membership"], "fireRank": fire_rank(ranks, rec["weekTotal"]),
                "tokens": {"input": 0, "output": 0, "cacheCreation": 0, "cacheRead": 0, "total": rec["weekTotal"]},
                "estCostUsd": None, "lastOfferingAt": rec["lastOfferingAt"], "flags": rec["flags"],
            }
    for k, e in week_seed.items():
        week_map[k] = e  # 教祖の今週分 seed を復元
    weekly = list(week_map.values())
    weekly.sort(key=lambda e: e["tokens"]["total"], reverse=True)
    for i, e in enumerate(weekly):
        e["rank"] = i + 1

    return {
        "schema": "banzuke/v1",
        "updatedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "measurement": "claude-code-only",
        "disclaimer": DISCLAIMER,
        "source": "static",
        "periods": {"total": total, "weekly": weekly},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="生成後に git commit && push する")
    args = ap.parse_args()

    ranks = load_ranks()
    data = build(ranks)
    with open(BANZUKE_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    n = len(data["periods"]["total"])
    print(f"data/banzuke.json を再生成: 累計 {n} 名 / 今週 {len(data['periods']['weekly'])} 名")

    if args.push:
        subprocess.run(["git", "-C", ROOT, "add", "data/banzuke.json"], check=False)
        subprocess.run(["git", "-C", ROOT, "commit", "-m", "chore: 御焚き上げ番付データ更新"], check=False)
        subprocess.run(["git", "-C", ROOT, "push"], check=False)
        print("git push 済み。")


if __name__ == "__main__":
    main()
