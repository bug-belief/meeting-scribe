#!/usr/bin/env bash
# =============================================================================
# 录音转写应用 · 服务端一次性初始化（必须 root 执行）
#
#   sudo bash ~/bin/meeting-setup.sh                # 默认顺带设置 tailscale operator=ai
#   sudo bash ~/bin/meeting-setup.sh --no-operator  # 不动 tailscale 权限
#
# 做的事（全部幂等，重复执行安全）：
#   1. 建一个专用系统用户（家目录落在容量足够的分区；本仓所有 unit 用 %h 指代它）
#   2. 把 ai 的现有公钥装进 meeting 的 authorized_keys（复用同一把钥匙，不新建密钥）
#   3. 建目录：~/app（代码）、~/data/{audios_inbox,processed,work,models,logs,tmp}
#   4. loginctl enable-linger meeting（重启后用户级 systemd 服务能自启）
#   5. tailscale set --operator=ai（让 ai 免 sudo 配 serve/funnel）
#   不安装任何 Python 包，不碰任何现有服务与容器。
# =============================================================================
set -euo pipefail

APP_USER="meeting"
APP_HOME="/home/${APP_USER}"
OPERATOR_USER="${OPERATOR_USER:-ai}"
PUBKEY="${PUBKEY:-}"          # 留空则运行时从 $OPERATOR_USER 的 authorized_keys 读取

WITH_OPERATOR=1
for arg in "$@"; do
  case "$arg" in
    --no-operator) WITH_OPERATOR=0 ;;
    --pubkey)   PUBKEY="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

log()  { printf '\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "请用 sudo 执行：sudo bash $0"

# ---------- 1. 用户 ----------
if id "$APP_USER" >/dev/null 2>&1; then
  log "用户 $APP_USER 已存在（uid=$(id -u "$APP_USER")），跳过创建"
else
  log "创建用户 $APP_USER"
  useradd -m -s /bin/bash "$APP_USER"
fi
getent passwd "$APP_USER"

# ---------- 2. SSH 公钥 ----------
# 复用 $OPERATOR_USER 已有的公钥：**运行时从它的 authorized_keys 读**，不硬编码。
#
# 为什么不写死一串：写死等于把你自己的公钥装到别人的服务器上 ——
# 谁跑这个脚本，谁的机器就多了一把不属于他的钥匙。原意是「复用现有钥匙」，
# 那就该从钥匙现在所在的地方读。
log "配置 $APP_USER 的 SSH 公钥（复用 $OPERATOR_USER 的现有密钥）"

if [ -z "$PUBKEY" ]; then
  OP_HOME=$(getent passwd "$OPERATOR_USER" | cut -d: -f6 || true)
  OP_AK="${OP_HOME}/.ssh/authorized_keys"
  if [ -n "${OP_HOME:-}" ] && [ -f "$OP_AK" ]; then
    PUBKEY=$(grep -m1 -E '^(ssh-(rsa|ed25519)|ecdsa-)' "$OP_AK" || true)
  fi
fi
if [ -z "$PUBKEY" ]; then
  die "找不到可用的 SSH 公钥。三选一：
     ① 先给 $OPERATOR_USER 配好公钥（脚本会复用它）
     ② --pubkey 'ssh-ed25519 AAAA... you@host'
     ③ PUBKEY='ssh-ed25519 AAAA...' sudo -E bash $0"
fi
log "公钥来源：${PUBKEY%% *} …（${PUBKEY##* }）"

install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$APP_HOME/.ssh"
AK="$APP_HOME/.ssh/authorized_keys"
touch "$AK"
if grep -qF "$PUBKEY" "$AK" 2>/dev/null; then
  log "公钥已存在，不重复追加"
else
  echo "$PUBKEY" >> "$AK"
  log "公钥已追加"
fi
chown "$APP_USER:$APP_USER" "$AK"
chmod 600 "$AK"
ls -l "$AK"

# ---------- 3. 目录 ----------
log "创建目录结构"
install -d -m 755 -o "$APP_USER" -g "$APP_USER" "$APP_HOME/app"
install -d -m 700 -o "$APP_USER" -g "$APP_USER" "$APP_HOME/data"
for d in audios_inbox processed work models logs tmp; do
  install -d -m 750 -o "$APP_USER" -g "$APP_USER" "$APP_HOME/data/$d"
done
# install -d 会自动创建父目录且属主为 root，这里统一纠正
chown -R "$APP_USER:$APP_USER" "$APP_HOME/data"
chmod 700 "$APP_HOME/data"
find "$APP_HOME" -maxdepth 2 -mindepth 1 -type d -printf '%M %u:%g %p\n' | sort

# ---------- 4. linger ----------
log "开启 linger（用户级 systemd 可跨登录常驻）"
loginctl enable-linger "$APP_USER"
loginctl show-user "$APP_USER" --property=Linger

# ---------- 5. tailscale operator ----------
if [ "$WITH_OPERATOR" -eq 1 ]; then
  if command -v tailscale >/dev/null 2>&1; then
    log "设置 tailscale operator = $OPERATOR_USER"
    tailscale set --operator="$OPERATOR_USER" || warn "设置 operator 失败（不影响后续，只是 serve/funnel 仍需 sudo）"
  else
    warn "未找到 tailscale 命令，跳过 operator 设置"
  fi
else
  log "按要求跳过 tailscale operator 设置"
fi

# ---------- 汇总 ----------
cat <<EOF

=========================== 完成 ===========================
用户      : $APP_USER（uid=$(id -u "$APP_USER")，无 sudo 权限）
家目录    : $APP_HOME
代码目录  : $APP_HOME/app
数据目录  : $APP_HOME/data （inbox/processed/work/models/logs/tmp）
磁盘      : $(df -h "$APP_HOME" | tail -1)
linger    : $(loginctl show-user "$APP_USER" --property=Linger --value)
operator  : $([ "$WITH_OPERATOR" -eq 1 ] && echo "$OPERATOR_USER" || echo "未设置")

下一步（回到 Windows 本机执行）：
  ssh <你的主机> "sudo -u $APP_USER bash -lc 'cd ~/app && pwd'"
  scp -r <项目目录>/. <你的主机>:/tmp/meeting-src/
===========================================================
EOF
