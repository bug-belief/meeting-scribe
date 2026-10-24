#!/usr/bin/env bash
# =============================================================================
# 录音转写应用 · 服务端引导脚本（以 meeting 用户身份运行，不需要 sudo）
#
# 用法：
#   bash deploy/bootstrap.sh dirs        # 建目录
#   bash deploy/bootstrap.sh uv          # 装 uv（用户级，~/.local/bin）
#   bash deploy/bootstrap.sh venv        # 建 .venv（Python 3.12）
#   bash deploy/bootstrap.sh deps        # 装依赖（默认 CPU 版，先跑通）
#   bash deploy/bootstrap.sh deps-gpu    # 换成 CUDA 12.4 版 torch
#   bash deploy/bootstrap.sh models      # 预下载 ASR 模型（约 1.2 GB）
#   bash deploy/bootstrap.sh check       # 环境自检
#   bash deploy/bootstrap.sh all         # dirs+uv+venv+deps+check
#
# 约定：代码在 ~/app，数据在 ~/data，互不混放
# =============================================================================
set -euo pipefail

APP_ROOT="${APP_ROOT:-$HOME/app}"
DATA_ROOT="${DATA_ROOT:-$HOME/data}"
VENV="$APP_ROOT/.venv"
PYPI_MIRROR="${PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
TORCH_CUDA_INDEX="https://download.pytorch.org/whl/cu124"

log()  { printf '\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

stage_dirs() {
  log "创建目录"
  mkdir -p "$APP_ROOT" \
           "$DATA_ROOT"/{audios_inbox,processed,work,models,logs,tmp}
  chmod 700 "$DATA_ROOT"
  ls -ld "$APP_ROOT" "$DATA_ROOT" "$DATA_ROOT"/*
}

stage_uv() {
  if command -v uv >/dev/null 2>&1; then
    log "uv 已存在：$(uv --version)"
    return
  fi
  log "安装 uv（用户级）"
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || fail "uv 安装后仍不在 PATH"
  uv --version
}

stage_venv() {
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null || fail "先跑 uv 阶段"
  if [ -x "$VENV/bin/python" ]; then
    log "venv 已存在：$("$VENV/bin/python" -V 2>&1)"
    return
  fi
  log "创建 venv（Python 3.12）"
  cd "$APP_ROOT"
  UV_INDEX_URL="$PYPI_MIRROR" uv venv --python 3.12 "$VENV"
  "$VENV/bin/python" -V
}

_pip() {
  export PATH="$HOME/.local/bin:$PATH"
  UV_INDEX_URL="$PYPI_MIRROR" uv pip install --python "$VENV/bin/python" "$@"
}

stage_deps() {
  log "安装基础依赖（走清华源）"
  _pip --upgrade pip setuptools wheel >/dev/null
  _pip numpy soundfile watchdog python-dotenv openai httpx
  log "安装 FunASR 运行时（首次会拉 modelscope / funasr-onnx 等）"
  _pip funasr modelscope
  log "torch 未装，先跑 check 会提示；要 GPU 请执行 deps-gpu"
}

stage_deps_gpu() {
  log "安装 torch cu124（约 2.5 GB，慢）"
  _pip --index-url "$TORCH_CUDA_INDEX" torch torchaudio
  stage_deps
  "$VENV/bin/python" - <<'PY'
import torch
print("torch", torch.__version__, "cuda_available:", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device:", torch.cuda.get_device_name(0))
PY
}

stage_models() {
  export PATH="$HOME/.local/bin:$PATH"
  export MODELSCOPE_CACHE="$DATA_ROOT/models"
  log "预下载模型到 $MODELSCOPE_CACHE"
  "$VENV/bin/python" - <<'PY'
import os
from modelscope import snapshot_download
targets = ["iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
           "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
           "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch",
           "iic/speech_campplus_sv_zh-cn_16k-common"]
for m in targets:
    p = snapshot_download(m, cache_dir=os.environ["MODELSCOPE_CACHE"])
    print("OK", m, "->", p)
PY
}

stage_check() {
  export PATH="$HOME/.local/bin:$PATH"
  log "环境自检"
  "$VENV/bin/python" - <<'PY'
import importlib, sys
mods = ["numpy", "soundfile", "watchdog", "dotenv", "openai", "funasr", "modelscope", "torch"]
for m in mods:
    try:
        mod = importlib.import_module(m)
        print(f"  {m:12s} OK  {getattr(mod, '__version__', '')}")
    except Exception as e:
        print(f"  {m:12s} MISSING ({type(e).__name__})")
try:
    import torch
    print("  cuda:", torch.cuda.is_available(), "| cuda_ver:", torch.version.cuda)
except Exception:
    pass
print("python:", sys.version.split()[0])
PY
  echo "--- 磁盘 ---"; df -h "$HOME" | tail -1
  echo "--- GPU ---";  command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=memory.free --format=csv,noheader || echo "无 nvidia-smi"
  echo "--- ffmpeg ---"; ffmpeg -version 2>/dev/null | head -1 || echo "缺 ffmpeg"
}

case "${1:-all}" in
  dirs)     stage_dirs ;;
  uv)       stage_uv ;;
  venv)     stage_venv ;;
  deps)     stage_deps ;;
  deps-gpu) stage_deps_gpu ;;
  models)   stage_models ;;
  check)    stage_check ;;
  all)      stage_dirs; stage_uv; stage_venv; stage_deps; stage_check ;;
  *)        fail "未知阶段：$1（可选 dirs/uv/venv/deps/deps-gpu/models/check/all）" ;;
esac
