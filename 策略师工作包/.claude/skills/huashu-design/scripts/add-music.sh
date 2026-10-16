#!/usr/bin/env bash
# Mix a background-music track into an MP4 video.
#
# 本工作包不带背景音乐：音乐文件由用户提供，用 --music=<path> 指定。
# 没给音乐文件、或给的文件找不到：不报错，提示一句并原样复制一份不带音乐的影片。
#
# Usage:
#   bash add-music.sh <input.mp4> --music=<path> [--out=<path>]
#
# Flags:
#   --music=<path>    the audio file to mix in (provided by the user)
#   --mood=<name>     legacy preset name; presets are no longer bundled, so this only produces the hint
#   --out=<path>      output path (default: <input-basename>-bgm.mp4)
#
# Legacy positional form still works: bash add-music.sh in.mp4 music.mp3 out.mp4
#
# Behavior:
#   - Music is trimmed to match video duration
#   - 0.3s fade in, 1.0s fade out (avoids hard cuts)
#   - Video stream copied (no re-encode), audio AAC 192k
#
# Examples:
#   bash add-music.sh my.mp4 --music=path/to/song.mp3
#   bash add-music.sh my.mp4 --music=path/to/song.mp3 --out=final.mp4
#
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ASSETS_DIR="$SCRIPT_DIR/../assets"

# ── Parse args ───────────────────────────────────────────────────────
INPUT=""
MOOD="tech"
CUSTOM_MUSIC=""
OUTPUT=""
POSITIONAL=()

for arg in "$@"; do
  case "$arg" in
    --mood=*)  MOOD="${arg#*=}" ;;
    --music=*) CUSTOM_MUSIC="${arg#*=}" ;;
    --out=*)   OUTPUT="${arg#*=}" ;;
    *)         POSITIONAL+=("$arg") ;;
  esac
done

# Legacy positional: <input> [music] [output]
INPUT="${POSITIONAL[0]}"
[ -z "$CUSTOM_MUSIC" ] && [ -n "${POSITIONAL[1]}" ] && CUSTOM_MUSIC="${POSITIONAL[1]}"
[ -z "$OUTPUT" ]       && [ -n "${POSITIONAL[2]}" ] && OUTPUT="${POSITIONAL[2]}"

if [ -z "$INPUT" ] || [ ! -f "$INPUT" ]; then
  echo "Usage: bash add-music.sh <input.mp4> [--mood=<name>] [--music=<path>] [--out=<path>]" >&2
  exit 1
fi

# ── Resolve music source: --music wins, else --mood（旧版预设；新包不带，仅在 assets/ 里有同名文件时才用）──
CUSTOM_GIVEN=""
if [ -n "$CUSTOM_MUSIC" ]; then
  MUSIC="$CUSTOM_MUSIC"
  SOURCE_LABEL="custom: $MUSIC"
  CUSTOM_GIVEN="1"
else
  MUSIC="$ASSETS_DIR/bgm-${MOOD}.mp3"
  SOURCE_LABEL="mood: $MOOD"
fi

# ── Resolve output path ─────────────────────────────────────────────
INPUT_DIR="$(cd "$(dirname "$INPUT")" && pwd)"
INPUT_NAME="$(basename "$INPUT" .mp4)"
[ -z "$OUTPUT" ] && OUTPUT="$INPUT_DIR/$INPUT_NAME-bgm.mp4"

if [ ! -f "$MUSIC" ]; then
  if [ -n "$CUSTOM_GIVEN" ]; then
    echo "提示：找不到你指定的音乐文件（${MUSIC}），工作包也不带背景音乐，这次的影片先不加音乐。" >&2
    echo "  （给 Agent：核对 --music= 的路径后重新运行）" >&2
  else
    echo "提示：工作包不带背景音乐，这次的影片先不加音乐。想加的话，请你提供一个音乐文件。" >&2
    echo "  （给 Agent：用户给了音乐文件后，重新运行并加 --music=<音乐文件>）" >&2
  fi
  if [ "$INPUT" != "$OUTPUT" ]; then cp "$INPUT" "$OUTPUT"; fi
  echo "✓ 已原样复制一份不带音乐的影片：${OUTPUT}"
  exit 0
fi

# ── Measure video duration, compute fade-out start ──────────────────
DURATION=$(ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "$INPUT")
if [ -z "$DURATION" ]; then
  echo "✗ Could not read video duration" >&2
  exit 1
fi
FADE_OUT_START=$(awk "BEGIN { d = $DURATION - 1; if (d < 0) d = 0; print d }")

echo "▸ Mixing BGM into video"
echo "  input:    $INPUT"
echo "  music:    $SOURCE_LABEL"
echo "  duration: ${DURATION}s"
echo "  output:   $OUTPUT"

ffmpeg -y -loglevel error \
  -i "$INPUT" \
  -i "$MUSIC" \
  -filter_complex "[1:a]atrim=0:${DURATION},asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.3,afade=t=out:st=${FADE_OUT_START}:d=1[a]" \
  -map 0:v -map "[a]" \
  -c:v copy -c:a aac -b:a 192k -shortest \
  "$OUTPUT"

SIZE=$(du -h "$OUTPUT" | cut -f1)
echo "✓ Done: $OUTPUT ($SIZE)"
