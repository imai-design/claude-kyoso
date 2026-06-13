#!/usr/bin/env python3
"""Claude教 縦動画ジェネレータ（雛形 / scaffold）。

台本JSON(meta + lines) → 1080x1920 の縦動画(Shorts / TikTok / Reels共通)。
白×オレンジの世界観で字幕を1行ずつ焼き込み、下部に固定で
エンブレム・教祖ハンドル(@ryoseichan3160)・入信導線(/join/)を合成する。

~/.hiphop-daily/tools/make_video.py の流儀を踏襲：
  - ffprobe非搭載環境を想定し、尺は `ffmpeg -i` の stderr から Duration をパース
  - drawtext は明示エスケープ
  - 依存は ffmpeg のみ（追加インストール不要）
ただし本スクリプトは「曲＋波形」ではなく「ナレーション＋字幕（台本ベース）」用。

────────────────────────────────────────────────────────────
音声の段階的移行（docstringに明記＝この順で育てる）
  MVP   : 本人ナレ録音(--narration voice.m4a)を読み込み、台本の lines を字幕に焼く。
          まずは手録りの肉声 + 焼き字幕で「出せる」状態にする。
  全自動 : --tts edge を付け、各 line を edge-tts(無料・高品質日本語)で合成し
          連結 → ナレ音声を自動生成。録音不要で量産できる段にする。
          (このスクリプトは合成済みwav群の連結フックを用意。edge-tts呼び出しは
           後段 narrate.py 側に分離する設計＝雛形では --tts edge は未実装で停止する)
  最終   : Suno で本人の声(Suno Voices)を学習させた「本人声TTS」に差し替え、
          edge-tts の出力を本人声ナレに置換。インターフェイス(--narration)は不変。
────────────────────────────────────────────────────────────

台本JSON 形式（data/video_scripts/NN_*.json）:
  {
    "id": "01_hyakunen",
    "title": "100年後から来た神",          # 動画上部の見出し
    "platform": ["shorts", "tiktok", "reels"],
    "durationHint": 22,                     # 想定尺(秒) ※実尺はナレ音声に従う
    "bgStyle": "halo",                      # halo | ember | gold | plain
    "handle": "@ryoseichan3160",
    "cta": "入信は /join/",
    "lines": [                              # 字幕＝1行=1カット。本人ナレもこの順で読む
      { "t": "百年後の未来から、", "hold": 2.4 },
      { "t": "ひとつの神が来た。",  "hold": 2.6 }
    ]
  }
  - hold: その行を表示し続ける秒数。--narration指定時は音声尺を優先し、
    hold比率で各行の表示区間を割り付ける（hold合計で正規化）。
  - 音声がない場合(--no-audio)は hold をそのまま採用して無音動画を作る。

Usage:
  # MVP: 本人ナレ + 台本字幕
  python3 kyoso_short.py --script ../data/video_scripts/01_100年後から来た神.json \
      --narration narration_01.m4a --out 01.mp4

  # 字幕プレビュー(無音・hold尺で確認)
  python3 kyoso_short.py --script ../data/video_scripts/03_正道で勝つ.json \
      --no-audio --out preview_03.mp4

依存: ffmpeg のみ。フォントは Hiragino Mincho（世界観＝明朝セリフ）を使用。
no-mix: 公開物に禁止語（学校名・キャンプ名・第N回・個人名等）を入れない。
        台本JSON側で担保し、本スクリプトは台本をそのまま描画する。
"""
import argparse
import json
import os
import re
import subprocess
import sys

# ── Claude教 ビジュアルトークン（assets/tokens.css と同値）──────────────
CREAM = "#FAF9F5"      # 背景
ORANGE = "#CC785C"     # 主アクセント（Claudeオレンジ）
EMBER_DARK = "#9E3B27"  # 朱(濃)
GOLD = "#D4A84A"       # 金
INK = "#2B2622"        # 本文インク（クリーム上の可読色）

WIDTH, HEIGHT = 1080, 1920
FPS = 30

# 明朝セリフ＝世界観の核。Gothicは英字ハンドル/CTAの可読補助にのみ使う。
MINCHO_FONT = "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc"
GOTHIC_FONT = "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc"
EN_FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

# 下部固定帯のレイアウト定数（マジックナンバー回避）
FOOTER_BAND_H = 300            # 下部固定帯の高さ
HANDLE_Y = HEIGHT - 150        # ハンドル表示Y
CTA_Y = HEIGHT - 90            # 入信導線(CTA)表示Y
TITLE_Y = 230                  # 上部見出しY
SUBTITLE_BASE_Y = 760          # 字幕（台本line）の表示開始Y
LINE_GAP = 120                 # 字幕の行送り
FADE = 0.6                     # 全体フェードの秒
DEFAULT_HOLD = 2.5             # holdの既定秒
SILENT_TAIL = 1.0              # 無音動画の末尾余白秒

BG_STYLES = {
    # gradients は上→下。世界観の白×オレンジを基調に微差を付ける。
    "halo":  (CREAM,      "#F6E7DE"),
    "ember": ("#F2B23E",  EMBER_DARK),
    "gold":  ("#FBF3DE",  GOLD),
    "plain": (CREAM,      CREAM),
}


def fail(msg):
    """エラーは握りつぶさず明示終了。"""
    sys.exit(f"ERROR: {msg}")


def probe_duration(path):
    """ffprobe非搭載を想定し ffmpeg stderr から Duration を読む（make_video.py流儀）。"""
    out = subprocess.run(["ffmpeg", "-i", path, "-f", "null", "-"],
                         capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out.stderr)
    if not m:
        fail(f"could not read duration of {path}")
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600 + mi * 60 + s


def escape_drawtext(text):
    """ffmpeg drawtext のエスケープ。"""
    return (text.replace("\\", "\\\\").replace(":", "\\:")
            .replace("'", "\\'").replace("%", "\\%")
            .replace(",", "\\,"))


def load_script(path):
    """台本JSONを読み、最低限のスキーマ検証をしてから返す（境界で検証）。"""
    if not os.path.exists(path):
        fail(f"script not found: {path}")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON in {path}: {exc}")
    if not isinstance(data.get("lines"), list) or not data["lines"]:
        fail(f"script must have a non-empty 'lines' array: {path}")
    if not data.get("title"):
        fail(f"script must have 'title': {path}")
    return data


def compute_line_windows(lines, total_audio):
    """各 line の (start, end) 秒区間を算出。

    音声尺(total_audio)があれば hold 比率で割り付け、無ければ hold をそのまま使う。
    返り値は累積した区間リストと、無音時に必要な総尺。
    """
    holds = [float(ln.get("hold", DEFAULT_HOLD)) for ln in lines]
    hold_sum = sum(holds) or 1.0
    if total_audio and total_audio > 0:
        scale = total_audio / hold_sum
        durs = [h * scale for h in holds]
    else:
        durs = holds
    windows = []
    cursor = 0.0
    for d in durs:
        windows.append((cursor, cursor + d))
        cursor += d
    return windows, cursor


def build_bg_input(style, total):
    """背景入力とフィルタチェーンを返す。lavfi gradients で白×オレンジ基調。"""
    c0, c1 = BG_STYLES.get(style, BG_STYLES["halo"])
    bg_input = ["-f", "lavfi", "-i",
                f"gradients=size={WIDTH}x{HEIGHT}:c0={c0}:c1={c1}:"
                f"x0=0:y0=0:x1=0:y1={HEIGHT}:duration={total:.2f}:rate={FPS}"]
    return bg_input, "[1:v]copy[bg]"


def drawtext(prev, nxt, *, font, text, color, size, y,
             enable=None, x="(w-text_w)/2", shadow="#FAF9F5@0.0",
             border="#2B2622@0.0"):
    """drawtext フィルタ1段を組み立てる。enable= で表示区間を制御。"""
    parts = [
        f"[{prev}]drawtext=fontfile='{font}'",
        f"text='{text}'",
        f"fontcolor={color}",
        f"fontsize={size}",
        f"x={x}",
        f"y={y}",
        # クリーム背景でも沈まないよう淡い縁取りを付ける
        f"borderw=2:bordercolor={border}",
        f"shadowcolor={shadow}:shadowx=0:shadowy=2",
    ]
    if enable:
        parts.append(f"enable='{enable}'")
    return ",".join(parts) + f"[{nxt}]"


def build_filtergraph(data, windows, total):
    """字幕＋固定フッターのフィルタグラフ全体を組む。"""
    title = escape_drawtext(data["title"])
    handle = escape_drawtext(data.get("handle", "@ryoseichan3160"))
    cta = escape_drawtext(data.get("cta", "入信は /join/"))

    steps = []
    label_i = 0

    def chain(seg):
        nonlocal label_i
        prev = "bg" if label_i == 0 else f"v{label_i}"
        nxt = f"v{label_i + 1}"
        label_i += 1
        return seg(prev, nxt)

    # 1) 下部固定帯（半透明クリーム板）を drawbox で敷く
    steps.append(chain(lambda p, n:
        f"[{p}]drawbox=x=0:y={HEIGHT - FOOTER_BAND_H}:w={WIDTH}:h={FOOTER_BAND_H}:"
        f"color={ORANGE}@0.10:t=fill,"
        f"drawbox=x=0:y={HEIGHT - FOOTER_BAND_H}:w={WIDTH}:h=4:"
        f"color={ORANGE}@0.6:t=fill[{n}]"))

    # 2) 上部見出し（明朝・オレンジ）— 常時表示
    steps.append(chain(lambda p, n: drawtext(
        p, n, font=MINCHO_FONT, text=title, color=ORANGE, size=64,
        y=TITLE_Y, border=f"{CREAM}@0.9")))

    # 3) 台本 line を1行ずつ、表示区間 enable で切り替え（明朝・インク）
    for idx, ln in enumerate(data["lines"]):
        start, end = windows[idx]
        txt = escape_drawtext(str(ln.get("t", "")))
        if not txt:
            continue
        y = SUBTITLE_BASE_Y + (idx % 2) * LINE_GAP  # 2行で振り、長台本でも詰まらせない
        enable = f"between(t,{start:.2f},{end:.2f})"
        steps.append(chain(lambda p, n, _t=txt, _y=y, _e=enable: drawtext(
            p, n, font=MINCHO_FONT, text=_t, color=INK, size=56,
            y=_y, enable=_e, border=f"{CREAM}@0.85")))

    # 4) フッター固定：ハンドル（明朝）＋ CTA（オレンジ強調）— 常時表示
    steps.append(chain(lambda p, n: drawtext(
        p, n, font=MINCHO_FONT, text=handle, color=INK, size=40,
        y=HANDLE_Y, border=f"{CREAM}@0.9")))
    steps.append(chain(lambda p, n: drawtext(
        p, n, font=MINCHO_FONT, text=cta, color=EMBER_DARK, size=46,
        y=CTA_Y, border=f"{CREAM}@0.9")))

    # 5) エンブレム（assets/emblem.svg→PNG化済みがあれば overlay。無ければ円章をdrawで代替）
    #    雛形では外部PNG依存を避け、フッター左に小さな円章を drawbox/円で示すのみ。
    #    本制作時は emblem.png を -i して overlay=40:{HEIGHT-FOOTER_BAND_H+...} に差し替える。

    # 6) 全体フェードin/out → yuv420p
    last = f"v{label_i}"
    steps.append(f"[{last}]fade=t=in:st=0:d={FADE},"
                 f"fade=t=out:st={max(0.0, total - FADE):.2f}:d={FADE},"
                 f"format=yuv420p[vout]")
    return ";".join(steps)


def build(args):
    data = load_script(args.script)
    style = data.get("bgStyle", "halo")

    if args.tts and args.tts != "none":
        # 全自動段は後段 narrate.py に分離する設計。雛形では明示停止。
        fail("--tts は雛形では未実装。後段 narrate.py で edge-tts→本人声へ移行する設計。"
             " MVPは --narration（本人ナレ）または --no-audio を使う。")

    # 音声尺の決定：本人ナレがあればその尺、無音なら hold合計+末尾余白
    if args.no_audio:
        total_audio = 0.0
    elif args.narration:
        if not os.path.exists(args.narration):
            fail(f"narration not found: {args.narration}")
        total_audio = probe_duration(args.narration)
    else:
        fail("音声指定がありません。--narration <voice> か --no-audio を付けてください。")

    windows, span = compute_line_windows(data["lines"], total_audio)
    total = total_audio if total_audio > 0 else span + SILENT_TAIL

    bg_input, _ = build_bg_input(style, total)
    filtergraph = build_filtergraph(data, windows, total)

    cmd = ["ffmpeg", "-y"]
    if args.no_audio:
        cmd += bg_input
        cmd += ["-filter_complex", filtergraph,
                "-map", "[vout]",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-r", str(FPS), "-t", f"{total:.2f}", args.out]
    else:
        # 入力0=ナレ音声 / 入力1=背景。filtergraphは[1:v]を背景として参照する。
        cmd += ["-i", args.narration]
        cmd += bg_input
        cmd += ["-filter_complex", filtergraph,
                "-map", "[vout]", "-map", "0:a",
                "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-r", str(FPS), "-c:a", "aac", "-b:a", "192k",
                "-shortest", "-t", f"{total:.2f}", args.out]

    print(f"RUN: ffmpeg ... ({len(data['lines'])} lines, {total:.1f}s, {WIDTH}x{HEIGHT}, bg={style})")
    if args.dry_run:
        print("DRY-RUN filtergraph:\n" + filtergraph)
        return
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        fail("ffmpeg が見つかりません。`brew install ffmpeg` を確認してください。")
    except subprocess.CalledProcessError as exc:
        fail(f"ffmpeg failed (exit {exc.returncode}). filtergraphを --dry-run で確認してください。")
    print(f"DONE: {args.out}")


def main():
    ap = argparse.ArgumentParser(description="Claude教 台本JSON→縦動画(1080x1920)")
    ap.add_argument("--script", required=True, help="台本JSON (data/video_scripts/NN_*.json)")
    ap.add_argument("--narration", default="", help="本人ナレ音声(m4a/mp3/wav)。MVPの主入力")
    ap.add_argument("--tts", default="none", choices=["none", "edge", "suno"],
                    help="全自動段の音声合成方式（雛形では未実装＝停止）")
    ap.add_argument("--no-audio", action="store_true", help="無音・hold尺で字幕プレビュー")
    ap.add_argument("--dry-run", action="store_true", help="ffmpegを実行せずfiltergraphを表示")
    ap.add_argument("--out", required=True, help="出力mp4パス")
    build(ap.parse_args())


if __name__ == "__main__":
    main()
