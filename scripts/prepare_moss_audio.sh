#!/bin/bash
# 8 场 AliMeeting 远场 → 单声道 16k wav（与产品 hyp_*_avg.mp3 同混音口径：8 通道平均下混）
# 用法：scripts/prepare_moss_audio.sh [输出目录，默认 /tmp/moss_direct_audio]
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"   # ffmpeg 不在非交互 shell 的默认 PATH 里
repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
src_dir="$repo_dir/evaluation/asr/first_test/audio/Eval_Ali/Eval_Ali_far/audio_dir"
out_dir="${1:-/tmp/moss_direct_audio}"
mkdir -p "$out_dir"
for src in "$src_dir"/*.wav; do
  name=$(basename "$src")
  meeting=${name%%_MS*}
  out="$out_dir/$meeting.wav"
  if [ -s "$out" ]; then
    echo "skip $meeting（已存在 $(stat -c%s "$out") bytes）"
    continue
  fi
  ffmpeg -y -v error -i "$src" -vn -ac 1 -ar 16000 -c:a pcm_s16le "$out"
  echo "made $out（$(stat -c%s "$out") bytes）"
done
echo "PREPARE_MOSS_AUDIO_DONE"
