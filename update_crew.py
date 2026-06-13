#!/usr/bin/env python3
"""Claude教 クルー員数カウンター更新スクリプト（data/crew.json）。

用途:
    LP やダッシュボードに出すクルー総数 / 建国メンバー残枠を一元更新する。
    実数がまだ薄い立ち上げ期は「○○人参加中」の素のカウンターを出すと
    かえって失速して見える。そこで本スクリプトは下記の表示ロジックを内蔵する。

決定（表示ロジック）:
    - 実数（current）が表示しきい値 REVEAL_THRESHOLD 未満の間は、
      総数カウンターを「非表示」にする（displayCounter=false）。
    - 代わりに常に『建国メンバー 残り◯枠/100』の希少性表現を前面に出す
      （foundingRemaining / foundingTotal）。
    - 実数がしきい値以上になったら自動でカウンターを解禁（displayCounter=true）。
    フロント側はこのフラグと文言フィールドを読むだけでよい（判定を埋めない）。

設計方針:
    - 標準ライブラリのみ（外部依存なし）。
    - immutable: 既存 JSON を読み、新しい dict を組んで書き戻す
      （元の dict をその場で書き換えない）。
    - 入力は境界で検証（負数・上限超過・型不正は弾く）。

このスクリプトは crew.json の更新のみ。git commit / push / 外部送信はしない前提。
（更新後、人が内容を確認してから手で commit する運用）

使い方:
    python3 update_crew.py                          # 現状を表示するだけ（変更なし）
    python3 update_crew.py --current 42             # クルー総数を 42 に更新
    python3 update_crew.py --founding-remaining 80  # 建国メンバー残枠を 80 に
    python3 update_crew.py --add 3                  # 総数を +3（建国残枠も連動 -3）
    python3 update_crew.py --current 1200 --dry-run # 変更内容を表示するだけ
"""

from __future__ import annotations  # Python 3.9 互換

import argparse
import json
import sys
from datetime import date
from pathlib import Path

# crew.json の場所（このスクリプトと同じルート配下の data/）。
DEFAULT_CREW_PATH = Path(__file__).resolve().parent / "data" / "crew.json"

# 建国メンバー枠の総数（canon：100）。
FOUNDING_TOTAL = 100

# クルー目標総数（canon：1万人）。
CREW_TARGET = 10000

# 素のカウンターを出してよくなる実数のしきい値。
#   これ未満は「残り◯枠/100」の希少性表現のみを前面に出す。
REVEAL_THRESHOLD = 100


def _coerce_int(value: object, *, field: str) -> int:
    """JSON 由来の値を非負 int へ。型不正なら例外。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} が整数ではありません: {value!r}")
    n = int(value)
    if n < 0:
        raise ValueError(f"{field} が負の値です: {n}")
    return n


def load_crew(path: Path) -> dict:
    """crew.json を読み込む。無ければ canon 既定で初期化した dict を返す。"""
    if not path.exists():
        return {
            "current": 0,
            "target": CREW_TARGET,
            "foundingTotal": FOUNDING_TOTAL,
            "foundingRemaining": FOUNDING_TOTAL,
            "lastUpdated": date.today().isoformat(),
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("crew.json のトップが object ではありません")
    return data


def compute_state(
    *,
    current: int,
    founding_remaining: int,
    founding_total: int = FOUNDING_TOTAL,
    target: int = CREW_TARGET,
) -> dict:
    """与えられた数から、フロントが読むだけで済む新しい状態 dict を組む。

    希少性表現と displayCounter フラグをここで確定させる（判定の単一真実源）。
    immutable: 新しい dict を返す。
    """
    current = max(0, current)
    founding_total = max(1, founding_total)
    founding_remaining = min(max(0, founding_remaining), founding_total)

    display_counter = current >= REVEAL_THRESHOLD
    founding_filled = founding_total - founding_remaining

    scarcity_label = f"建国メンバー 残り{founding_remaining}枠/{founding_total}"

    return {
        "current": current,
        "target": target,
        "foundingTotal": founding_total,
        "foundingRemaining": founding_remaining,
        "foundingFilled": founding_filled,
        # フロントはこのフラグだけ見ればよい：
        #   true  → 総数カウンターを表示
        #   false → 希少性表現（scarcityLabel）のみ表示
        "displayCounter": display_counter,
        "revealThreshold": REVEAL_THRESHOLD,
        "scarcityLabel": scarcity_label,
        "lastUpdated": date.today().isoformat(),
    }


def print_state(state: dict) -> None:
    """現在の状態を読みやすく表示する。"""
    print("\n=== Claude教 クルー員数 ===")
    print(f"  クルー総数   : {state['current']} / 目標 {state['target']}")
    print(
        f"  建国メンバー : 残り {state['foundingRemaining']} 枠 / "
        f"{state['foundingTotal']}（埋まり {state.get('foundingFilled', '?')}）"
    )
    shown = "表示" if state.get("displayCounter") else "非表示（希少性表現のみ）"
    print(f"  カウンター   : {shown}（解禁しきい値 {state.get('revealThreshold', REVEAL_THRESHOLD)}）")
    print(f"  前面の文言   : {state.get('scarcityLabel', '?')}")
    print(f"  更新日       : {state['lastUpdated']}\n")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="Claude教 crew.json を更新（希少性表現ロジック込み・commit はしない）"
    )
    ap.add_argument("--path", default=str(DEFAULT_CREW_PATH), help="crew.json のパス")
    ap.add_argument("--current", type=int, help="クルー総数を絶対値で設定")
    ap.add_argument("--add", type=int, help="クルー総数を相対加算（建国残枠も連動して減る）")
    ap.add_argument(
        "--founding-remaining",
        type=int,
        help="建国メンバー残枠を絶対値で設定（0〜100）",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="変更内容を表示するだけで書き込まない"
    )
    args = ap.parse_args(argv)

    path = Path(args.path)
    try:
        data = load_crew(path)
        current = _coerce_int(data.get("current", 0), field="current")
        founding_total = _coerce_int(
            data.get("foundingTotal", FOUNDING_TOTAL), field="foundingTotal"
        )
        founding_remaining = _coerce_int(
            data.get("foundingRemaining", founding_total),
            field="foundingRemaining",
        )
        target = _coerce_int(data.get("target", CREW_TARGET), field="target")
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: crew.json の読み込みに失敗: {exc}", file=sys.stderr)
        return 2

    # 引数が一切無ければ現状表示のみ。
    no_change_args = args.current is None and args.add is None and args.founding_remaining is None
    if no_change_args:
        print_state(
            compute_state(
                current=current,
                founding_remaining=founding_remaining,
                founding_total=founding_total,
                target=target,
            )
        )
        print("（変更引数なし：現状表示のみ。--current / --add / --founding-remaining で更新）")
        return 0

    # 値の適用（immutable：ローカル変数を更新して compute_state に渡す）。
    if args.add is not None:
        delta = args.add
        current = max(0, current + delta)
        # 建国期は参加が即建国枠を消費する想定で残枠も連動させる。
        founding_remaining = max(0, founding_remaining - max(0, delta))

    if args.current is not None:
        if args.current < 0:
            print("ERROR: --current は 0 以上で指定してください", file=sys.stderr)
            return 2
        current = args.current

    if args.founding_remaining is not None:
        if not (0 <= args.founding_remaining <= founding_total):
            print(
                f"ERROR: --founding-remaining は 0〜{founding_total} で指定してください",
                file=sys.stderr,
            )
            return 2
        founding_remaining = args.founding_remaining

    new_state = compute_state(
        current=current,
        founding_remaining=founding_remaining,
        founding_total=founding_total,
        target=target,
    )

    print_state(new_state)

    if args.dry_run:
        print("DRY-RUN：書き込みは行いませんでした。")
        return 0

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(new_state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"更新しました: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
