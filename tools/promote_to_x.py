#!/usr/bin/env python3
"""Claude教 七火位 昇格検知 → X 下書きペイロード生成（雛形）。

用途:
    御焚き上げ番付（data/banzuke.json）で、あるクルーの火位（七火位：
    火口→灯火→篝火→焚火→焔→大火→熱波）が前回より上がったのを検知し、
    昇格を祝う OGP / X 投稿文面を生成する。文面は必ず nomix_guard を通し、
    禁止語が混ざっていれば破棄する。

    生成物は autopost.py が読める下書きペイロード（JSON ＋ --file 用テキスト）
    としてローカルに書き出すだけ。auto_post は常に false 固定で、
    この雛形は実投稿・送信・git commit を一切しない前提。
    （承認ゲート：人が文面を確認し、自分で autopost.py を叩いて投稿する）

検知の仕組み:
    - 「前回の火位スナップショット」（--prev-state JSON）と
      現在の banzuke.json を突き合わせ、火位 index が上がった handle を昇格とみなす。
    - --prev-state を渡さなければ、初回として「現在の火位に到達したお披露目」
      を全員ぶん生成する（--announce-all 指定時のみ。既定は安全側で何も出さない）。

設計方針:
    - 標準ライブラリのみ（外部依存なし）。Python 3.9 互換。
    - immutable: 入力 JSON は読むだけ。出力は新規ファイルに書く。
    - nomix_guard はサブモジュール import（同梱の tools/nomix_guard.py）。

使い方:
    # まず現在の火位スナップショットを保存（次回の差分基準にする）
    python3 tools/promote_to_x.py --snapshot tools/.fire_state.json

    # 前回スナップショットと比べて昇格者の下書きを生成
    python3 tools/promote_to_x.py \
        --prev-state tools/.fire_state.json \
        --out-dir tools/promotions

    # 後日 人が確認して投稿（このスクリプトは投稿しない）
    #   python3 autopost.py --file tools/promotions/<handle>.txt
"""

from __future__ import annotations  # Python 3.9 互換

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# 同梱の nomix_guard を import するためパスを通す。
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

from nomix_guard import scan as nomix_scan  # noqa: E402

REPO_ROOT = _THIS_DIR.parent
DEFAULT_BANZUKE = REPO_ROOT / "data" / "banzuke.json"
DEFAULT_RANKS = _THIS_DIR / "ofumi" / "ranks.json"
LP_URL = "https://imai-design.github.io/claude-kyoso/banzuke/"

# autopost.py のスレッド区切り（--file は "\n---\n" で分割する）。
THREAD_SEP = "\n---\n"


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"見つかりません: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"object ではありません: {path}")
    return data


def load_fire_ranks(ranks_path: Path) -> list[dict]:
    """七火位の定義を min 昇順で返す（index が大きいほど上位）。"""
    data = load_json(ranks_path)
    ranks = data.get("ranks")
    if not isinstance(ranks, list) or not ranks:
        raise ValueError(f"ranks 配列がありません: {ranks_path}")
    return sorted(ranks, key=lambda r: r.get("min", 0))


def rank_index(ranks: list[dict], rank_id: str) -> int:
    """火位 id の序列 index（不明なら -1）。"""
    for i, r in enumerate(ranks):
        if r.get("id") == rank_id:
            return i
    return -1


def current_fire_state(banzuke: dict) -> dict:
    """banzuke.json から {handle: fireRank_id} の現在状態を抽出する。"""
    state: dict = {}
    periods = banzuke.get("periods", {})
    total = periods.get("total", [])
    for entry in total:
        handle = entry.get("handle")
        fire = entry.get("fireRank")
        if handle and fire:
            state[handle] = fire
    return state


def build_drafts(
    *,
    promotions: list[dict],
    ranks: list[dict],
) -> list[dict]:
    """昇格情報から X 下書き（文面＋メタ）を組む。nomix で弾かれた分は除外。

    promotions: [{"handle":..., "from": id|None, "to": id}]
    Returns: [{handle, fromRank, toRank, text, thread, status, violations}]
    """
    by_id = {r["id"]: r for r in ranks}
    drafts: list[dict] = []

    for p in promotions:
        handle = p["handle"]
        to_id = p["to"]
        to_rank = by_id.get(to_id, {})
        from_id = p.get("from")
        from_rank = by_id.get(from_id, {}) if from_id else {}

        text = _compose_text(handle, from_rank, to_rank)
        thread = _compose_thread(handle, from_rank, to_rank)

        # 本文・スレッド全部を nomix ゲートに通す（混在は破棄）。
        joined = text + "\n" + THREAD_SEP.join(thread)
        violations = nomix_scan(joined)
        status = "ready" if not violations else "blocked"

        drafts.append(
            {
                "handle": handle,
                "fromRank": from_rank.get("name"),
                "toRank": to_rank.get("name"),
                "text": text,
                "thread": thread,
                "status": status,
                "violations": [str(v) for v in violations],
            }
        )
    return drafts


def _compose_text(handle: str, from_rank: dict, to_rank: dict) -> str:
    """昇格祝いの単発ポスト文面（荘厳＋遊び心、絵文字は使わない）。"""
    to_name = to_rank.get("name", "?")
    glyph = to_rank.get("glyph", "")
    tagline = to_rank.get("tagline", "")
    from_name = from_rank.get("name")

    arrow = f"{from_name} ▸ {to_name}" if from_name else to_name
    lines = [
        f"［御焚き上げ番付・昇格の報せ ─ {glyph}］",
        "",
        f"{handle} が火位を上げた。",
        f"  {arrow}",
        f"  ── {tagline}",
        "",
        "より多く焚べし者ほど信仰篤し。",
        f"番付を見る → {LP_URL}",
    ]
    return "\n".join(lines)


def _compose_thread(handle: str, from_rank: dict, to_rank: dict) -> list[str]:
    """スレッド版（autopost.py --file の \\n---\\n 区切りで使う想定）。"""
    head = _compose_text(handle, from_rank, to_rank)
    follow = (
        "御焚き上げ番付は、クロード卿に焚べたトークン（薪）の累計で決まる"
        "七火位の信仰等級。\n"
        "課金階層とは別軸＝学徒でも焚べた者が上に立つ。"
    )
    return [head, follow]


def detect_promotions(
    *,
    prev_state: dict,
    curr_state: dict,
    ranks: list[dict],
    announce_all: bool,
) -> list[dict]:
    """前回と現在の火位を比べ、上がった handle を抽出する。

    prev_state が空かつ announce_all のときは、現在の火位を「お披露目」扱いで全件出す。
    """
    promotions: list[dict] = []
    for handle, curr_id in sorted(curr_state.items()):
        prev_id = prev_state.get(handle)
        curr_idx = rank_index(ranks, curr_id)
        if prev_id is None:
            # 初登場：announce_all のときだけお披露目として出す。
            if announce_all and curr_idx > 0:
                promotions.append({"handle": handle, "from": None, "to": curr_id})
            continue
        prev_idx = rank_index(ranks, prev_id)
        if curr_idx > prev_idx:
            promotions.append({"handle": handle, "from": prev_id, "to": curr_id})
    return promotions


def write_payloads(drafts: list[dict], out_dir: Path) -> list[Path]:
    """ready な下書きを autopost.py --file 用テキスト＋メタ JSON で書き出す。

    auto_post は常に false 固定（このスクリプトは投稿しない）。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    now = datetime.now().astimezone().isoformat()

    for d in drafts:
        safe_handle = "".join(
            ch if ch.isalnum() or ch in "-_あ-んア-ン一-龥ぁ-ゖ" else "_"
            for ch in d["handle"]
        )[:40] or "crew"

        # autopost.py --file 用テキスト（スレッドは \n---\n 区切り）。
        txt_path = out_dir / f"{safe_handle}.txt"
        body = d["text"] if d["status"] == "ready" else ""
        if d["status"] == "ready":
            txt_path.write_text(THREAD_SEP.join(d["thread"]), encoding="utf-8")
            written.append(txt_path)

        # メタ JSON（人が確認するための監査ペイロード）。
        meta_path = out_dir / f"{safe_handle}.json"
        payload = {
            "schema": "promote-to-x/v1",
            "generatedAt": now,
            "handle": d["handle"],
            "fromRank": d["fromRank"],
            "toRank": d["toRank"],
            "status": d["status"],
            "nomixViolations": d["violations"],
            # 投稿安全弁：必ず false。実投稿は人が autopost.py を叩く。
            "auto_post": False,
            "autopostCommand": (
                f"python3 autopost.py --thread --file {txt_path}"
                if d["status"] == "ready"
                else None
            ),
            "text": d["text"],
            "thread": d["thread"],
        }
        meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        written.append(meta_path)
        _ = body  # （未使用変数の明示。将来 body 直渡しする場合のフック）
    return written


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description="七火位 昇格検知→X下書き生成（nomix通過・auto_post:false固定・投稿しない）"
    )
    ap.add_argument("--banzuke", default=str(DEFAULT_BANZUKE), help="banzuke.json のパス")
    ap.add_argument("--ranks", default=str(DEFAULT_RANKS), help="七火位定義 ranks.json")
    ap.add_argument(
        "--prev-state",
        help="前回の火位スナップショット JSON（{handle: fireRankId}）",
    )
    ap.add_argument(
        "--snapshot",
        help="現在の火位を指定パスへスナップショット保存して終了（差分基準作り）",
    )
    ap.add_argument(
        "--out-dir",
        default=str(_THIS_DIR / "promotions"),
        help="下書きペイロードの出力先ディレクトリ",
    )
    ap.add_argument(
        "--announce-all",
        action="store_true",
        help="prev-state が無い handle も現火位のお披露目として出す",
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="検知結果を表示するだけで書き出さない"
    )
    args = ap.parse_args(argv)

    try:
        banzuke = load_json(Path(args.banzuke))
        ranks = load_fire_ranks(Path(args.ranks))
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: 入力の読み込みに失敗: {exc}", file=sys.stderr)
        return 2

    curr_state = current_fire_state(banzuke)

    # スナップショット保存モード（差分基準を作るだけ）。
    if args.snapshot:
        snap_path = Path(args.snapshot)
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(
            json.dumps(curr_state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"火位スナップショットを保存: {snap_path}（{len(curr_state)}名）")
        return 0

    prev_state: dict = {}
    if args.prev_state:
        try:
            prev_state = load_json(Path(args.prev_state))
        except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: prev-state の読み込みに失敗: {exc}", file=sys.stderr)
            return 2

    promotions = detect_promotions(
        prev_state=prev_state,
        curr_state=curr_state,
        ranks=ranks,
        announce_all=args.announce_all,
    )

    if not promotions:
        print("昇格なし（差分検知の結果、火位が上がったクルーはいません）。")
        return 0

    drafts = build_drafts(promotions=promotions, ranks=ranks)

    print(f"\n=== 昇格検知 {len(drafts)}件 ===")
    for d in drafts:
        flag = "OK" if d["status"] == "ready" else "BLOCKED(no-mix)"
        frm = f"{d['fromRank']} ▸ " if d["fromRank"] else ""
        print(f"  [{flag}] {d['handle']}: {frm}{d['toRank']}")
        if d["violations"]:
            for v in d["violations"]:
                print(f"      ✗ {v}")

    if args.dry_run:
        print("\nDRY-RUN：ペイロードは書き出しませんでした。")
        return 0

    out_dir = Path(args.out_dir)
    written = write_payloads(drafts, out_dir)
    print(f"\n書き出し: {out_dir}（{len(written)}ファイル）")
    print("投稿は人が確認のうえ autopost.py を叩く（このスクリプトは投稿しない）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
