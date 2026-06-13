#!/usr/bin/env python3
"""Claude教 no-mix ゲート ─ 公開物に絶対書いてはいけない禁止語の共通フィルタ。

用途:
    神託・動画台本・X投稿文面・OGP・ニュースレター（NL）など、
    Claude教の名で外に出る全自動出力が「必ず一度通す」検閲ゲート。
    関係先（学校・FC・関係者個人）への迷惑を構造的に防ぐための最終防波堤。

設計方針:
    - import して使う共通関数（scan / assert_clean / sanitize_or_raise）が本体。
    - CLI は確認・CI 用。標準ライブラリのみ（外部依存なし）。
    - ヒットしたら例外送出 or 破棄（黙って通すことは絶対にしない）。

このスクリプトは「検閲のみ」。実行・投稿・送信・git commit は一切しない前提。

使い方（CLI）:
    python3 nomix_guard.py path/to/draft.md          # ファイルを検査（NGなら exit 1）
    python3 nomix_guard.py --text "検査したい文字列"   # 文字列を直接検査
    cat draft.md | python3 nomix_guard.py -           # 標準入力を検査
    python3 nomix_guard.py --list                     # 禁止語ルール一覧を表示

使い方（import）:
    from nomix_guard import assert_clean, scan, is_clean
    assert_clean(text)          # NGなら NoMixViolation を送出
    hits = scan(text)           # ヒット一覧（list[Violation]）を返す（送出しない）
    if is_clean(text): ...      # bool 判定
"""

# Python 3.9 互換：list | None 等の新表記を遅延評価にする（殿の環境は 3.9.6）。
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# 禁止語ルール定義
#   - literals: 単純な部分一致（大文字小文字・全角半角の揺れを正規化して比較）
#   - patterns: 正規表現で拾うパターン（「第N回」等の可変表記）
#   ここを増やすときは label を一意にし、必ず公開物全種で再検査すること。
# ─────────────────────────────────────────────────────────────────────────────

# 単純な禁止文字列（正規化後に部分一致で検出）
#   正規化は「小文字化＋全角→半角＋連続空白を1個に圧縮」を行うため、
#   ここには「正規化後の形（小文字・半角・空白は1個）」を書くこと。
#   例: "claude camp" は元が "Claude  Camp" でも一致する。
FORBIDDEN_LITERALS: tuple[tuple[str, str], ...] = (
    ("school_name", "ホリエモンai学校"),
    ("school_name_short", "ホリエモンai"),
    ("school_kana", "ホリエモンエーアイ"),
    ("camp_hyphen", "claude-camp"),
    ("camp_space", "claude camp"),  # "Claude Camp" 等（空白は正規化で1個に）
    ("camp_nospace", "claudecamp"),
    ("camp_ja", "claudeキャンプ"),
    ("camp_ja_kana", "クロードキャンプ"),
    ("full_max_nospace", "fullmax"),
    ("full_max_space", "full max"),  # "Full Max"（空白形）
    ("full_max_ja", "フルマックス"),
    ("camp_prefix", "camp-"),
)

# 正規表現で拾う可変パターン（(label, 説明, コンパイル済みパターン)）
FORBIDDEN_PATTERNS: tuple[tuple[str, str, "re.Pattern[str]"], ...] = (
    (
        "lecture_count",
        "講義回数の単独表記（第N回 / 第N講 / Day単独回数表記）",
        # 「第12回」「第3講」「第 5 回」等。算用数字・漢数字の両対応。
        re.compile(r"第\s*[0-9０-９一二三四五六七八九十百]+\s*[回講]"),
    ),
    (
        "camp_code",
        "camp-NN 系の内部コード名（camp-01, camp-12 等）",
        re.compile(r"camp[-_][0-9]{1,3}", re.IGNORECASE),
    ),
)

# 許可されている表記（誤検知を避けるためのホワイトリスト・参考用）
ALLOWED_TERMS: tuple[str, ...] = (
    "教祖",
    "両聖ちゃん",
    "伝令",
)


def _normalize(text: str) -> str:
    """比較用に正規化する。

    - 小文字化（英字の大小揺れ吸収）
    - 全角英数→半角（ｃｌａｕｄｅ 等の回避を防ぐ）
    - 連続空白の圧縮（c l a u d e camp のような分断回避）
    """
    # 全角英数記号 → 半角
    out_chars = []
    for ch in text:
        code = ord(ch)
        # 全角 ! (0xFF01) 〜 ~ (0xFF5E) を半角へ
        if 0xFF01 <= code <= 0xFF5E:
            out_chars.append(chr(code - 0xFEE0))
        # 全角スペース → 半角
        elif code == 0x3000:
            out_chars.append(" ")
        else:
            out_chars.append(ch)
    normalized = "".join(out_chars).lower()
    # 連続する空白類を1つに（分断による回避を潰す）
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized


@dataclass(frozen=True)
class Violation:
    """検出された禁止語1件。"""

    label: str
    matched: str
    kind: str  # "literal" or "pattern"
    description: str = ""

    def __str__(self) -> str:
        desc = f" — {self.description}" if self.description else ""
        return f"[{self.kind}:{self.label}] 「{self.matched}」{desc}"


class NoMixViolation(Exception):
    """禁止語が見つかったことを示す例外。

    violations 属性に検出された Violation のリストを持つ。
    全自動パイプラインはこの例外を捕捉して当該出力を破棄すること。
    """

    def __init__(self, violations: list[Violation]):
        self.violations = violations
        joined = "; ".join(str(v) for v in violations)
        super().__init__(f"no-mix 違反 {len(violations)}件: {joined}")


def scan(text: str) -> list[Violation]:
    """禁止語を走査し、ヒット一覧を返す（例外は送出しない）。

    immutable: 入力 text を変更せず、新規リストを返す。
    """
    if text is None:
        return []
    normalized = _normalize(text)
    found: list[Violation] = []

    for label, term in FORBIDDEN_LITERALS:
        needle = _normalize(term)
        if needle and needle in normalized:
            found.append(
                Violation(label=label, matched=term, kind="literal")
            )

    # パターンは元テキストに対しても、正規化テキストに対しても照合する
    # （正規化で漢字等は保持されるため日本語パターンも有効）
    for label, desc, pattern in FORBIDDEN_PATTERNS:
        for source in (text, normalized):
            m = pattern.search(source)
            if m:
                found.append(
                    Violation(
                        label=label,
                        matched=m.group(0),
                        kind="pattern",
                        description=desc,
                    )
                )
                break  # 同一ラベルの重複報告を避ける
    return found


def is_clean(text: str) -> bool:
    """禁止語が無ければ True。"""
    return len(scan(text)) == 0


def assert_clean(text: str) -> str:
    """検査して問題なければ text をそのまま返す。NGなら NoMixViolation を送出。

    パイプライン内で「通った値だけが次工程に進む」よう、戻り値で素通しする。
    """
    violations = scan(text)
    if violations:
        raise NoMixViolation(violations)
    return text


def sanitize_or_raise(text: str, *, label: str = "output") -> str:
    """assert_clean の別名（呼び出し意図を明示したいとき用）。

    黙ってマスクして通すのではなく、必ず例外で止める方針。
    （禁止語を伏字にして公開する運用は誤公開の温床になるため採用しない）
    """
    violations = scan(text)
    if violations:
        # どの出力で起きたか追跡できるようラベルを添えて送出する。
        exc = NoMixViolation(violations)
        exc.source_label = label
        raise exc
    return text


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def _read_input(args: argparse.Namespace) -> tuple[str, str]:
    """(source_label, text) を返す。"""
    if args.text is not None:
        return ("--text", args.text)
    target = args.path
    if target == "-" or target is None:
        return ("<stdin>", sys.stdin.read())
    p = Path(target)
    if not p.exists():
        print(f"ERROR: ファイルが見つかりません: {target}", file=sys.stderr)
        sys.exit(2)
    return (str(p), p.read_text(encoding="utf-8"))


def _print_rules() -> None:
    print("\n=== no-mix 禁止語ルール ===\n")
    print("[literal] 部分一致（正規化後）:")
    for label, term in FORBIDDEN_LITERALS:
        print(f"  - {label}: 「{term}」")
    print("\n[pattern] 正規表現:")
    for label, desc, pattern in FORBIDDEN_PATTERNS:
        print(f"  - {label}: {desc}  /{pattern.pattern}/")
    print("\n[allowed] 許可表記（誤検知回避の参考）:")
    for term in ALLOWED_TERMS:
        print(f"  - 「{term}」")
    print()


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Claude教 no-mix 検閲ゲート（検査のみ・投稿/実行はしない）"
    )
    ap.add_argument(
        "path",
        nargs="?",
        help="検査するファイルパス（'-' で標準入力）",
    )
    ap.add_argument("--text", help="検査する文字列を直接指定")
    ap.add_argument("--list", action="store_true", help="禁止語ルール一覧を表示")
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="OK時に何も出力しない（CI向け）",
    )
    args = ap.parse_args(argv)

    if args.list:
        _print_rules()
        return 0

    if args.text is None and args.path is None:
        ap.error("検査対象がありません（ファイルパス / --text / '-' のいずれか）")

    source, text = _read_input(args)
    violations = scan(text)

    if violations:
        print(
            f"NG: no-mix 違反 {len(violations)}件 ({source})", file=sys.stderr
        )
        for v in violations:
            print(f"  ✗ {v}", file=sys.stderr)
        print(
            "  → この出力は公開してはいけません。当該箇所を削除してください。",
            file=sys.stderr,
        )
        return 1

    if not args.quiet:
        print(f"OK: no-mix 違反なし ({source})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
