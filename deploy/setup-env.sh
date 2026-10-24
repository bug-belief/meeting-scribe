set -euo pipefail
# 代码与数据都落在 home 下（默认 ~/app 与 ~/data）。
# 想放到别处就设 DATA_DIR，不用改本脚本。
#   例：DATA_DIR=/mnt/bigdisk/data bash deploy/setup-env.sh
APP="${APP_DIR:-$HOME/app}"
DATA="${DATA_DIR:-$HOME/data}"
ENVF="$APP/.env"

# 先读出现有 key（用户已填）
set -a; . "$ENVF"; set +a
: "${LLM_API_KEY:?未找到 LLM_API_KEY}"
echo "沿用现有 key：len=${#LLM_API_KEY} tail=${LLM_API_KEY: -4}"

# 覆盖前先记下现有 key 集合：本脚本生成的是「模板项」，如果旧文件里有
# 模板之外的项（后来手工加的），覆盖会让它们静默退回代码默认值 —— 其中
# WEB_TOKEN 退化成默认口令、ASSISTANT_HOMETOWN 退化成内置值，都是坑。
OLD_KEYS=$(grep -E "^[A-Z_]+=" "$ENVF" 2>/dev/null | cut -d= -f1 | sort || true)

umask 077
cat > "$ENVF" <<EOF
# ============================================================
# 录音转写应用 · 服务端配置（生成于 $(date '+%F %T')）
# 位置：$ENVF   权限：600（仅属主可读）
# 改完执行：systemctl --user restart meeting-watcher meeting-tts
# ============================================================

# ---------- 目录（绝对路径；由 DATA 变量展开，默认 ~/data）----------
INBOX_DIR=$DATA/audios_inbox
PROCESSED_DIR=$DATA/processed
WORK_DIR=$DATA/work
LOGS_DIR=$DATA/logs
MODELSCOPE_CACHE=$DATA/models
AUDIO_EXTS=.m4a,.mp3,.wav,.aac,.flac,.ogg,.opus,.amr,.wma,.mp4

# ---------- 触发判定 ----------
STABILITY_SECONDS=8
STABILITY_TIMEOUT=600
POLL_INTERVAL_SEC=60
MIN_FILE_SIZE_KB=8
MIN_DURATION_SEC=3
MAX_ATTEMPTS=3
RETRY_COOLDOWN_SEC=300

# ---------- ffmpeg ----------
FFMPEG_BIN=ffmpeg
FFPROBE_BIN=ffprobe
KEEP_WAV=false

# ---------- 本地 ASR ----------
ASR_BACKEND=funasr
ASR_DEVICE=cuda:0
ASR_LANGUAGE=zh
SPEAKER_LABEL_PREFIX=Speaker
ASR_SPLIT_MINUTES=0
FUNASR_MODEL=paraformer-zh
FUNASR_VAD=fsmn-vad
FUNASR_PUNC=ct-punc
FUNASR_SPK=cam++
FUNASR_BATCH_SIZE_S=120
FUNASR_HUB=ms

WHISPERX_MODEL=large-v3
WHISPERX_BATCH_SIZE=8
WHISPERX_DIARIZE=true
WHISPERX_MIN_SPEAKERS=0
WHISPERX_MAX_SPEAKERS=0
HF_TOKEN=

# ---------- 云端大模型（DeepSeek，仅 flash）----------
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=$LLM_API_KEY
LLM_MODEL=deepseek-flash
LLM_THINKING=off
LLM_ALLOW_NON_FLASH=false
LLM_TEMPERATURE=0.3
LLM_MAX_TOKENS=2048
LLM_TIMEOUT=180
LLM_RETRIES=3
LLM_PROXY=

# ---------- 报告分块 ----------
SUMMARY_DIRECT_CHARS=12000
SUMMARY_CHUNK_CHARS=6000

# ---------- 日志 ----------
LOG_LEVEL=INFO
EOF
chmod 600 "$ENVF"

# 自检：本次没写回的旧配置项（不静默丢配置）
NEW_KEYS=$(grep -E "^[A-Z_]+=" "$ENVF" | cut -d= -f1 | sort)
if [ -n "${OLD_KEYS:-}" ]; then
  LOST=$(comm -23 <(printf '%s\n' "$OLD_KEYS") <(printf '%s\n' "$NEW_KEYS") || true)
  if [ -n "$LOST" ]; then
    echo "⚠️  以下旧配置项未被写回（将退回代码默认值），请手工补进 $ENVF："
    printf '%s\n' "$LOST" | sed 's/^/   - /'
    echo "    （其中 WEB_TOKEN 缺失会退回默认口令，务必补上）"
  else
    echo "✅ 旧配置项已全部覆盖，无丢失"
  fi
fi

echo "--- 写入结果 ---"
ls -l "$ENVF"
grep -cE '^[A-Z_]+=' "$ENVF"
awk -F= '/^LLM_API_KEY=/{print "LLM_API_KEY 已写入，后四位 " substr($2,length($2)-3)}' "$ENVF"
