#!/usr/bin/env python3
"""Claude教 アクセス計測タグ 冪等注入スクリプト（Cloudflare Web Analytics）。

用途:
    サイト内の全 HTML ページの <head> に Cloudflare Web Analytics の
    beacon 1行を入れる。Cookie レス・同意バナー不要の計測を、自動集客の
    地盤として全ページへ横展開する。

    既に注入済みのページは二重に入れない（冪等）。
    beacon token はソースに焼き込まず、環境変数 or 引数から差し込む
    （プレースホルダ運用：token 未指定なら `__CF_BEACON_TOKEN__` を埋める）。

設計方針:
    - 標準ライブラリのみ（外部依存なし）。
    - distribute.py 方式で </head> 直前にタグを挿入する単純置換。
    - マーカーコメントで自分の注入箇所を識別し、再実行で更新/冪等を保つ。
    - immutable: ファイル内容は新しい文字列を生成して書き戻す（in-place な
      文字列変異はしない）。

このスクリプトは「HTML への計測タグ注入」のみを行う。
git commit / push / デプロイ / 外部送信は一切しない前提。
（--dry-run で差分確認 → 人が内容を見てから手で commit する運用）

token の渡し方（優先順）:
    1) --token "XXXX"            引数で明示
    2) 環境変数 CF_BEACON_TOKEN  例: export CF_BEACON_TOKEN=abcd1234
    3) どちらも無ければ          プレースホルダ `__CF_BEACON_TOKEN__` を残す
       （後から sed/置換で本番 token に差し替える運用）

使い方:
    python3 inject_analytics.py --dry-run            # 変更内容だけ表示（書かない）
    python3 inject_analytics.py                      # 全ページに注入（token はプレースホルダ）
    python3 inject_analytics.py --token abcd1234     # token を差し込んで注入
    CF_BEACON_TOKEN=abcd python3 inject_analytics.py # 環境変数経由
    python3 inject_analytics.py --root /path/to/site # 対象ルートを指定
    python3 inject_analytics.py --remove             # 注入タグを全ページから除去
"""

from __future__ import annotations  # Python 3.9 互換

import argparse
import os
import re
import sys
from pathlib import Path

# このスクリプトが置かれているディレクトリ＝サイトのルート（既定）。
DEFAULT_ROOT = Path(__file__).resolve().parent

# 注入ブロックを一意に識別するマーカー。再実行時はこの範囲を丸ごと置換する。
MARKER_BEGIN = "<!-- claude-kyoso:analytics:begin (auto-injected; do not edit by hand) -->"
MARKER_END = "<!-- claude-kyoso:analytics:end -->"

# token 未指定時に残すプレースホルダ。後から本番値に置換する。
TOKEN_PLACEHOLDER = "__CF_BEACON_TOKEN__"

# 走査から除外するディレクトリ（生成物・依存・履歴など）。
EXCLUDED_DIR_NAMES = frozenset(
    {".git", "node_modules", "worker", ".github", "__pycache__"}
)


def build_block(token: str) -> str:
    """注入する計測ブロック（マーカー込み）を生成して返す。

    Cloudflare Web Analytics は Cookie レス・同意バナー不要。
    defer 付き 1 行 beacon。token はデータ属性に JSON で渡す。
    """
    beacon = (
        '<script defer src="https://static.cloudflareinsights.com/beacon.min.js" '
        f'data-cf-beacon=\'{{"token": "{token}"}}\'></script>'
    )
    return f"{MARKER_BEGIN}\n{beacon}\n{MARKER_END}"


def _existing_block_pattern() -> "re.Pattern[str]":
    """既に注入済みブロック（マーカー間）を丸ごと拾う正規表現。"""
    return re.compile(
        re.escape(MARKER_BEGIN) + r".*?" + re.escape(MARKER_END),
        re.DOTALL,
    )


def remove_block(html: str) -> str:
    """注入済みブロックを除去した新しい HTML を返す（無ければそのまま）。"""
    pattern = _existing_block_pattern()
    # ブロック直後の余分な空行も1つ畳む。
    cleaned = pattern.sub("", html)
    return re.sub(r"\n{3,}", "\n\n", cleaned)


# 既存ブロック内の token 値を取り出す正規表現（冪等判定用）。
_TOKEN_IN_BLOCK = re.compile(r'"token":\s*"([^"]*)"')


def _token_in_block(block_text: str) -> str | None:
    """既存の注入ブロックから現在の token 値を抜き出す（無ければ None）。"""
    m = _TOKEN_IN_BLOCK.search(block_text)
    return m.group(1) if m else None


def inject(html: str, token: str) -> tuple[str, str]:
    """HTML に計測ブロックを冪等注入する。

    Returns:
        (new_html, action)
        action は "inserted" / "updated" / "skipped" / "no-head" のいずれか。

    immutable: 引数 html は変更せず、新しい文字列を返す。
    """
    block = build_block(token)
    pattern = _existing_block_pattern()

    existing = pattern.search(html)
    if existing:
        # 既存ブロックがある → token が同一なら何もしない（インデント差は無視）。
        # 違う token なら、既存ブロックのインデントを保ったまま token だけ差し替える。
        current_token = _token_in_block(existing.group(0))
        if current_token == token:
            return (html, "skipped")
        # 既存ブロックの体裁（インデント等）を保ったまま token 値だけ差し替える。
        updated_block = _TOKEN_IN_BLOCK.sub(
            lambda _m: f'"token": "{token}"', existing.group(0), count=1
        )
        return (pattern.sub(lambda _m: updated_block, html, count=1), "updated")

    # 新規挿入：</head> の直前に入れる（最初の 1 個のみ）。
    head_close = re.compile(r"</head>", re.IGNORECASE)
    m = head_close.search(html)
    if not m:
        return (html, "no-head")

    insert_at = m.start()
    # インデントは </head> 行の見た目に軽く合わせる。
    indented = "  " + block.replace("\n", "\n  ") + "\n"
    new_html = html[:insert_at] + indented + html[insert_at:]
    return (new_html, "inserted")


def iter_html_files(root: Path) -> list[Path]:
    """root 配下の .html を集める（除外ディレクトリはスキップ）。"""
    found: list[Path] = []
    for path in sorted(root.rglob("*.html")):
        parts = set(path.relative_to(root).parts)
        if parts & EXCLUDED_DIR_NAMES:
            continue
        found.append(path)
    return found


def resolve_token(arg_token: str | None) -> str:
    """token を「引数 > 環境変数 > プレースホルダ」の優先順で決める。"""
    if arg_token:
        return arg_token.strip()
    env_token = os.environ.get("CF_BEACON_TOKEN", "").strip()
    if env_token:
        return env_token
    return TOKEN_PLACEHOLDER


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="Claude教 全ページに Cloudflare Web Analytics を冪等注入"
        "（書き込みのみ・commit/デプロイはしない）"
    )
    ap.add_argument(
        "--root",
        default=str(DEFAULT_ROOT),
        help="サイトのルートディレクトリ（既定: このスクリプトの場所）",
    )
    ap.add_argument(
        "--token",
        default=None,
        help="Cloudflare beacon token（未指定なら CF_BEACON_TOKEN→プレースホルダ）",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="変更内容を表示するだけで書き込まない",
    )
    ap.add_argument(
        "--remove",
        action="store_true",
        help="注入済みの計測タグを全ページから除去する",
    )
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"ERROR: ルートが見つかりません: {root}", file=sys.stderr)
        return 2

    files = iter_html_files(root)
    if not files:
        print(f"対象 HTML が見つかりません: {root}", file=sys.stderr)
        return 1

    token = resolve_token(args.token)
    if not args.remove and token == TOKEN_PLACEHOLDER:
        print(
            "注意: token 未指定のためプレースホルダ "
            f"`{TOKEN_PLACEHOLDER}` を埋め込みます。"
            "本番前に実 token へ置換してください。",
            file=sys.stderr,
        )

    counts = {"inserted": 0, "updated": 0, "skipped": 0, "no-head": 0, "removed": 0}
    for path in files:
        rel = path.relative_to(root)
        original = path.read_text(encoding="utf-8")

        if args.remove:
            new_html = remove_block(original)
            action = "removed" if new_html != original else "skipped"
        else:
            new_html, action = inject(original, token)

        counts[action] = counts.get(action, 0) + 1
        changed = new_html != original

        if action == "no-head":
            print(f"  !  {rel}  ── <head> が無く注入できません")
            continue

        mark = "*" if changed else " "
        print(f"  {mark} [{action:<8}] {rel}")

        if changed and not args.dry_run:
            path.write_text(new_html, encoding="utf-8")

    mode = "DRY-RUN（未書き込み）" if args.dry_run else "適用済み"
    summary = ", ".join(f"{k}={v}" for k, v in counts.items() if v)
    print(f"\n{mode} ─ {len(files)}ページ走査  ({summary})")
    if not args.remove:
        print(f"  token = {token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
