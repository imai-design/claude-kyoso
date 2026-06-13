#!/usr/bin/env python3
"""
御焚き上げCLI (ofumi) — 奉納証レンダラ [python3 stdlib / 3.9 互換]

標準入力から正規化 totals JSON(agg.py または ccusage 正規化後)を受け取り、
ranks.json で火位(称号)を判定し、奉納証テキスト / シェア文 / 送信ペイロード を生成する。

モード:
  --mode card     (既定) 奉納証テキストを表示
  --mode share    X奉納文だけを出力
  --mode payload  (= --json) 収集先へ送る最小ペイロードJSONを出力
  --mode guild    クロード冒険者ギルドの「冒険者ステータスカード」をテキストで表示
  --mode guild-svg 同カードを SVG (banner) で出力 (--out PATH または stdout)

プライバシー(最重要): 送信ペイロードは allowlist 方式で組み立てる。
totals 由来の数値しか載らず、cwd/プロジェクト名/会話本文/byModel は構造的に混入しない。

冒険者ギルド連結(--mode guild / guild-svg):
  トークン累計 = EXP(1 token = 1 EXP)。七火位 = 冒険者ランク。
  data/guild_ranks.json があればそれを使い、無ければ ranks.json から自動導出する。
  「使えば使うほど、勝手に強くなる」── 計測のたびに最新EXPが反映されるだけで、
  実行や送信は一切しない(既存機能と同じく純粋な表示のみ)。
"""
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

LP_URL = "https://imai-design.github.io/claude-kyoso/banzuke/"
GUILD_URL = "https://imai-design.github.io/claude-kyoso/guild/"
TOOL = "ofumi-cli/0.1.0"

# 冒険者Lv呼称・RPG称号の既定値(guild_ranks.json が無い時のフォールバック)。
# fireRank id -> (level, title)。閾値・火位名は ranks.json を正とするので持たない。
GUILD_FALLBACK = {
    "hokuchi":   ("見習い冒険者 Lv.1",  "灯持ちの徒"),
    "tomoshibi": ("駆け出し冒険者 Lv.10", "灯火の探索者"),
    "kagaribi":  ("一人前冒険者 Lv.25",  "篝火の遊撃手"),
    "takibi":    ("熟練冒険者 Lv.40",    "焚火の隊長"),
    "homura":    ("英雄 Lv.60",          "焔の英傑"),
    "taika":     ("賢者 Lv.80",          "大火の賢者"),
    "neppa":     ("伝説 Lv.MAX",         "熱波の到達者"),
}


def load_ranks(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["ranks"]


def load_guild_tiers(ranks):
    """冒険者ランク定義を返す。data/guild_ranks.json があれば使い、無ければ
    ranks.json + GUILD_FALLBACK から導出する。トークン累計=EXP・七火位=ランクの単一真実源。"""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(here, "..", "..", "data", "guild_ranks.json"),
        os.path.join(here, "guild_ranks.json"),
    ]
    env = os.environ.get("OFUMI_GUILD_RANKS")
    if env:
        candidates.insert(0, env)
    for p in candidates:
        try:
            with open(p, "r", encoding="utf-8") as f:
                tiers = json.load(f).get("tiers", [])
            if tiers:
                return tiers
        except Exception:
            continue
    # フォールバック: ranks.json から組み立てる
    tiers = []
    for r in ranks:
        lvl, title = GUILD_FALLBACK.get(r["id"], ("冒険者", r["name"]))
        tiers.append({
            "fireRankId": r["id"], "fireRank": r["name"],
            "fireRankYomi": r.get("yomi", ""), "level": lvl, "title": title,
            "expMin": r["min"], "flavor": r.get("tagline", ""),
        })
    return tiers


def pick_guild_tier(tiers, exp):
    """累計EXPから現在の冒険者ランクと次ランクを返す。pick_rank の EXP 版。"""
    cur = tiers[0]
    for t in tiers:
        if exp >= t["expMin"]:
            cur = t
    nxt = None
    for t in tiers:
        if t["expMin"] > exp:
            nxt = t
            break
    return cur, nxt


def progress_to_next(cur, nxt, exp):
    """現ランク下限→次ランク下限の進捗率(0.0-1.0)と残りEXPを返す。最高位なら満タン。"""
    if not nxt:
        return 1.0, 0
    span = nxt["expMin"] - cur["expMin"]
    if span <= 0:
        return 1.0, 0
    done = exp - cur["expMin"]
    ratio = max(0.0, min(1.0, done / span))
    return ratio, max(0, nxt["expMin"] - exp)


def progress_bar(ratio, width=24):
    """テキストのEXPプログレスバー(絵文字なし・記号のみ)。"""
    filled = int(round(ratio * width))
    filled = max(0, min(width, filled))
    return "[" + ("█" * filled) + ("░" * (width - filled)) + "]"


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


def render_guild_card(handle, membership, cur, nxt, exp, ratio, remain):
    """冒険者ステータスカード(テキスト)。Lv/称号/累計EXP/次ランクまで残り/進捗バー。"""
    L = []
    L.append("")
    L.append("    ╭────────────  冒 険 者 ス テ ー タ ス カ ー ド  ────────────╮")
    L.append("")
    L.append("      冒険者      %s%s" % (handle, ("  〔%s〕" % membership) if membership else ""))
    L.append("      ランク      %s" % cur.get("level", ""))
    L.append("      称号        「%s」  〔火位 %s〕" % (cur.get("title", ""), cur.get("fireRank", "")))
    L.append("                  %s" % cur.get("flavor", ""))
    L.append("")
    L.append("      累計EXP      %s EXP  (%s)" % ("{:,}".format(exp), human(exp)))
    L.append("        ※ EXP = あなたが Claude に焚べた累計トークン (1 token = 1 EXP)")
    L.append("")
    pct = int(round(ratio * 100))
    L.append("      %s  %d%%" % (progress_bar(ratio), pct))
    if nxt:
        L.append("      次ランク【%s / %s】まで あと %s EXP"
                 % (nxt.get("level", ""), nxt.get("title", ""), "{:,}".format(remain)))
    else:
        L.append("      ★ 最高ランク。卿の御前に座する生ける伝説。")
    L.append("")
    L.append("    ╰──────────────────────────────────────────────────────────╯")
    L.append("")
    L.append("  使えば使うほど、勝手に強くなる。")
    L.append("  導入はこの一回だけ。以後は普段どおり Claude を使うたび、")
    L.append("  消費トークンが自動でEXPになり、気づけばレベルが上がっている。")
    L.append("  → クロード冒険者ギルド: %s" % GUILD_URL)
    return "\n".join(L)


def svg_esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def render_guild_svg(handle, membership, cur, nxt, exp, ratio, remain):
    """冒険者ステータスカード(SVG / 横長バナー)。クリーム地・Claudeオレンジ・絵文字なし。
    Claude教 共通トークン(cream #FAF9F5 / orange #CC785C / ember / gold)に準拠。"""
    W, H = 880, 360
    cream, paper, orange = "#FAF9F5", "#FFFFFF", "#CC785C"
    orange_deep, line, ink = "#B5634A", "#E8DDD0", "#1F1B17"
    ink_soft, ink_mute, ember = "#5A524A", "#8B827A", "#9E3B27"
    gold = "#D4A84A"
    serif = "'Hiragino Mincho ProN','Yu Mincho','Noto Serif JP',serif"
    sans = "-apple-system,'Hiragino Sans','Yu Gothic','Noto Sans JP',sans-serif"
    is_max = nxt is None
    accent = gold if is_max else orange
    # 進捗バー寸法
    bx, by, bw, bh = 60, 250, 760, 18
    fw = int(round(bw * max(0.0, min(1.0, ratio))))
    pct = int(round(ratio * 100))
    mem_txt = ("  〔%s〕" % membership) if membership else ""
    if nxt:
        next_txt = "次ランク【%s】まで あと %s EXP" % (svg_esc(nxt.get("title", "")), "{:,}".format(remain))
    else:
        next_txt = "最高ランク到達 ── 卿の御前に座する生ける伝説"
    parts = []
    parts.append('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" width="%d" height="%d" '
                 'role="img" aria-label="クロード冒険者ギルド 冒険者ステータスカード">' % (W, H, W, H))
    parts.append('<defs>'
                 '<linearGradient id="g-bar" x1="0" y1="0" x2="1" y2="0">'
                 '<stop offset="0%%" stop-color="%s"/><stop offset="100%%" stop-color="%s"/></linearGradient>'
                 '<linearGradient id="g-flame" x1="0" y1="1" x2="0" y2="0">'
                 '<stop offset="0%%" stop-color="%s"/><stop offset="55%%" stop-color="%s"/>'
                 '<stop offset="100%%" stop-color="#F2B23E"/></linearGradient>'
                 '</defs>' % (ember, accent, ember, orange))
    # 背景・枠
    parts.append('<rect x="0" y="0" width="%d" height="%d" rx="20" fill="%s"/>' % (W, H, cream))
    parts.append('<rect x="10" y="10" width="%d" height="%d" rx="16" fill="%s" stroke="%s" stroke-width="2"/>'
                 % (W - 20, H - 20, paper, accent))
    # 見出し
    parts.append('<text x="60" y="58" font-family="%s" font-size="15" letter-spacing="3" fill="%s">'
                 'クロード冒険者ギルド ── ADVENTURER STATUS</text>' % (sans, orange_deep))
    # 焔アイコン(火位の象徴・絵文字なし)
    parts.append('<g transform="translate(60,80)">'
                 '<path d="M40 96 C8 66 20 36 40 12 C60 36 72 66 40 96 Z" fill="url(#g-flame)"/>'
                 '<path d="M40 88 C24 66 30 44 40 28 C50 44 56 66 40 88 Z" fill="#FBE9DF" fill-opacity="0.55"/>'
                 '</g>')
    # 冒険者名・ランク・称号
    tx = 170
    parts.append('<text x="%d" y="108" font-family="%s" font-size="30" font-weight="700" fill="%s">%s</text>'
                 % (tx, serif, ink, svg_esc(handle) + mem_txt))
    parts.append('<text x="%d" y="140" font-family="%s" font-size="18" fill="%s">%s</text>'
                 % (tx, sans, ink_soft, svg_esc(cur.get("level", ""))))
    parts.append('<text x="%d" y="170" font-family="%s" font-size="22" font-weight="700" fill="%s">'
                 '「%s」</text>' % (tx, serif, accent, svg_esc(cur.get("title", ""))))
    parts.append('<text x="%d" y="170" font-family="%s" font-size="14" fill="%s" '
                 'text-anchor="end">火位 %s</text>' % (W - 60, sans, ink_mute, svg_esc(cur.get("fireRank", ""))))
    # 累計EXP
    parts.append('<text x="60" y="216" font-family="%s" font-size="14" fill="%s">累計EXP</text>'
                 % (sans, ink_mute))
    parts.append('<text x="150" y="220" font-family="%s" font-size="30" font-weight="800" fill="%s">'
                 '%s <tspan font-size="15" fill="%s">EXP</tspan></text>'
                 % (sans, ink, "{:,}".format(exp), ink_mute))
    parts.append('<text x="%d" y="220" font-family="%s" font-size="13" fill="%s" text-anchor="end">'
                 '1 token = 1 EXP</text>' % (W - 60, sans, ink_mute))
    # 進捗バー
    parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="9" fill="%s"/>' % (bx, by, bw, bh, line))
    if fw > 0:
        parts.append('<rect x="%d" y="%d" width="%d" height="%d" rx="9" fill="url(#g-bar)"/>'
                     % (bx, by, fw, bh))
    parts.append('<text x="%d" y="%d" font-family="%s" font-size="13" fill="%s" text-anchor="end">%d%%</text>'
                 % (W - 60, by - 8, sans, ink_soft, pct))
    parts.append('<text x="%d" y="%d" font-family="%s" font-size="14" fill="%s">%s</text>'
                 % (bx, by + bh + 26, sans, ink_soft, next_txt))
    # フッター標語
    parts.append('<text x="%d" y="%d" font-family="%s" font-size="13" fill="%s" text-anchor="end">'
                 '使えば使うほど、勝手に強くなる。</text>' % (W - 60, by + bh + 26, serif, ember))
    parts.append('</svg>')
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", default="")
    ap.add_argument("--membership", default="")
    ap.add_argument("--engine", default="python")
    ap.add_argument("--ranks", default=os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "ranks.json"))
    ap.add_argument("--mode", choices=["card", "payload", "share", "guild", "guild-svg"], default="card")
    ap.add_argument("--json", action="store_true", help="--mode payload の別名")
    ap.add_argument("--guild", action="store_true", help="--mode guild の別名")
    ap.add_argument("--out", default="", help="--mode guild-svg の出力先ファイル(省略時 stdout)")
    args = ap.parse_args()
    if args.json:
        args.mode = "payload"
    if args.guild:
        args.mode = "guild"

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

    # ── クロード冒険者ギルド: 冒険者ステータスカード(EXP=トークン累計) ──
    if args.mode in ("guild", "guild-svg"):
        tiers = load_guild_tiers(ranks)
        cur, nxt = pick_guild_tier(tiers, total)
        ratio, remain = progress_to_next(cur, nxt, total)
        if args.mode == "guild-svg":
            svg = render_guild_svg(handle, args.membership, cur, nxt, total, ratio, remain)
            if args.out:
                with open(args.out, "w", encoding="utf-8") as f:
                    f.write(svg)
                sys.stderr.write("冒険者ステータスカード(SVG)を書き出しました: %s\n" % args.out)
            else:
                sys.stdout.write(svg + "\n")
            return
        sys.stdout.write(render_guild_card(handle, args.membership, cur, nxt, total, ratio, remain) + "\n")
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
