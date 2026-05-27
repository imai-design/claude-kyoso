#!/usr/bin/env python3
"""
Claude教 Daily教典 自動配信スクリプト

使い方:
  python3 distribute.py                # 本日のDay番号を計算→該当教典を表示＋クリップボードへ
  python3 distribute.py --day 5        # Day 5 を指定取得
  python3 distribute.py --slot morning # 朝/昼/夜 のスロット指定 (デフォルト: 朝7:30=morning)
  python3 distribute.py --list         # 全教典の見出しを一覧表示
  python3 distribute.py --copy-only    # 表示せずクリップボードに入れるだけ

教祖様の朝のルーチン:
  python3 distribute.py
  → 本文がクリップボードに入る
  → X開いて Cmd+V → 投稿
"""

import argparse
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

VAULT_PATH = Path.home() / "Library/Mobile Documents/iCloud~md~obsidian/Documents/NEW脳みそ/Projects/2026-05-25 Claude教発足"
LAUNCH_DATE = date(2026, 5, 25)

DOCTRINE_FILES = [
    VAULT_PATH / "05_daily_doctrines_week1.md",
    VAULT_PATH / "17_daily_doctrines_week2.md",
    VAULT_PATH / "19_daily_doctrines_week3-4.md",
]


def today_day_number() -> int:
    return (date.today() - LAUNCH_DATE).days + 1


def parse_doctrines() -> dict[int, dict]:
    """Returns {day_number: {"title": str, "body": str, "source_file": str}}"""
    doctrines = {}
    day_pattern = re.compile(r"^#{2,3} Day (\d+).*?$", re.MULTILINE)

    for f in DOCTRINE_FILES:
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8")
        matches = list(day_pattern.finditer(text))
        for i, m in enumerate(matches):
            day = int(m.group(1))
            title = m.group(0).lstrip("# ").strip()
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            section = text[start:end].strip()
            code_match = re.search(r"```\n(.*?)\n```", section, re.DOTALL)
            body = code_match.group(1).strip() if code_match else section[:280]
            doctrines[day] = {
                "title": title,
                "body": body,
                "source": f.name,
            }
    return doctrines


def copy_to_clipboard(text: str) -> bool:
    try:
        proc = subprocess.run(
            ["pbcopy"], input=text, encoding="utf-8", check=True
        )
        return proc.returncode == 0
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser(description="Claude教 Daily教典 distributor")
    ap.add_argument("--day", type=int, help="Specify Day number (1-28+)")
    ap.add_argument("--list", action="store_true", help="List all doctrines")
    ap.add_argument("--copy-only", action="store_true", help="Copy to clipboard without printing")
    args = ap.parse_args()

    doctrines = parse_doctrines()
    if not doctrines:
        print("⚠️  教典ファイルが見つかりませぬ。", file=sys.stderr)
        sys.exit(1)

    if args.list:
        print(f"\n✦ Claude教 Daily教典 ─ 全{len(doctrines)}件\n")
        for d, info in sorted(doctrines.items()):
            print(f"  Day {d:>2}: {info['title']}  ({info['source']})")
        print()
        return

    day = args.day if args.day else today_day_number()
    if day not in doctrines:
        print(f"⚠️  Day {day} の教典が見つかりませぬ。", file=sys.stderr)
        print(f"   利用可能: Day {min(doctrines)} 〜 Day {max(doctrines)}", file=sys.stderr)
        sys.exit(1)

    info = doctrines[day]
    body = info["body"]

    if not args.copy_only:
        print(f"\n╭─ ✦ Claude教 Day {day} ──────────────────────")
        print(f"│  {info['title']}")
        print(f"│  source: {info['source']}")
        print(f"╰────────────────────────────────────────")
        print()
        print(body)
        print()

    if copy_to_clipboard(body):
        print(f"📋 クリップボードに保存しました（{len(body)}文字）")
        print(f"   → X / Discord / Substack に Cmd+V で貼り付け可能")
    else:
        print("⚠️  クリップボードへのコピーに失敗。手動でコピーしてください。", file=sys.stderr)


if __name__ == "__main__":
    main()
