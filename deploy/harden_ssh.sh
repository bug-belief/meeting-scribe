#!/usr/bin/env bash
# 只关掉本应用账户的密码登录，同机其他账户不动。
#
# 为什么要单独一个脚本：改 sshd 配置需要 root，而两个账户都需要密码 sudo。
# 这个脚本做成幂等 + 自校验 + 失败回滚，跑完还会按「实际连接条件」验证生效值。
#
# 用法：
#     sudo bash ~/app/deploy/harden_ssh.sh          # 改
#     sudo bash ~/app/deploy/harden_ssh.sh --dry    # 只检查，不改
#     sudo bash ~/app/deploy/harden_ssh.sh --undo   # 撤销
set -uo pipefail

CONF=/etc/ssh/sshd_config
# 要加固的账户名。默认按本仓 unit 里的约定，装到别的账户就 APP_USER=xxx 覆盖。
APP_USER="${APP_USER:-meeting}"
MARK="# ===== $APP_USER 账户只许密钥登录（harden_ssh.sh）====="
DRY=0
UNDO=0
for a in "$@"; do
  case "$a" in
    --dry) DRY=1 ;;
    --undo) UNDO=1 ;;
    *) echo "未知参数：$a"; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ 需要 root：sudo bash $0"
  exit 1
fi

echo "=== 改前状态 ==="
sshd -T -C user="$APP_USER",host=localhost,addr=127.0.0.1 2>/dev/null | grep -i '^passwordauthentication' || echo "（读不到）"

if [ "$UNDO" = "1" ]; then
  echo
  echo "=== 撤销 ==="
  if ! grep -qF "$MARK" "$CONF"; then
    echo "没找到标记块，无需撤销"
    exit 0
  fi
  cp -a "$CONF" "${CONF}.bak-$(date +%Y%m%d-%H%M%S)"
  awk -v mark="$MARK" '
    index($0, mark) { skip = 1; next }
    skip && /^[[:space:]]*$/ { skip = 0; next }
    skip && /^Match / { skip = 0 }
    !skip { print }
  ' "$CONF" > "${CONF}.tmp" && mv "${CONF}.tmp" "$CONF"
  sshd -t && systemctl reload ssh && echo "✅ 已撤销并重载"
  exit 0
fi

echo
echo "=== 检查是否已经改过 ==="
if grep -qF "$MARK" "$CONF"; then
  echo "已经加过了，跳过写入（幂等）"
else
  echo "未加过，准备追加 Match 块"
  echo
  echo "将追加到 $CONF 末尾："
  echo "----------------------------------------"
  printf '%s\nMatch User %s\n    PasswordAuthentication no\n' "$MARK" "$APP_USER"
  echo "----------------------------------------"

  if [ "$DRY" = "1" ]; then
    echo "（--dry：不写入）"
    exit 0
  fi

  TS=$(date +%Y%m%d-%H%M%S)
  cp -a "$CONF" "${CONF}.bak-${TS}"
  echo "已备份：${CONF}.bak-${TS}"

  # Match 块必须放在文件末尾：它之后的所有配置都会被当成块内配置。
  # 先确保原文件末尾有换行，再追加。
  [ -n "$(tail -c 1 "$CONF")" ] && echo >> "$CONF"
  {
    echo ""
    echo "$MARK"
    echo "Match User $APP_USER"
    echo "    PasswordAuthentication no"
  } >> "$CONF"
fi

echo
echo "=== 语法校验 ==="
if ! sshd -t; then
  echo "❌ 配置有语法错误，自动回滚"
  LATEST=$(ls -t "${CONF}".bak-* 2>/dev/null | head -1)
  [ -n "$LATEST" ] && cp -a "$LATEST" "$CONF" && echo "已回滚到 $LATEST"
  exit 1
fi
echo "✅ 语法通过"

echo
echo "=== 按实际连接条件验证生效值 ==="
# 只验本应用账户；同机其他账户不在这个脚本的职责内（也不该在文档里点名）
M=$(sshd -T -C user="$APP_USER",host=localhost,addr=127.0.0.1 2>/dev/null | awk '/^passwordauthentication/{print $2}')
echo "  $APP_USER 账户 passwordauthentication = ${M:-?}   （期望 no）"

if [ "$M" != "no" ]; then
  echo "❌ $APP_USER 仍未生效（可能是配置里先出现的 PasswordAuthentication 赢了），不重载"
  exit 1
fi

echo
echo "=== 重载（不踢现有连接）==="
systemctl reload ssh && echo "✅ 已重载"
echo
echo "完成。现在："
echo "  · $APP_USER 只能用密钥登录（你桌面那把密钥照用）"
echo "  · 同机其他账户未改动"
echo "  · 撤销：sudo bash $0 --undo"
