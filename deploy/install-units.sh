#!/usr/bin/env bash
# ============================================================================ #
# 把 deploy/*.service 与 *.timer 装到 ~/.config/systemd/user/ 并做展开断言。
#
# 【为什么要断言，而不只是 cp】
#   这些 unit 用 `%h`（systemd specifier = 当前用户 home）代替写死的 /home/xxx。
#   `%h` 在 **--user unit** 里 = 当前用户 home，这是对的；但如果在
#   `/etc/systemd/system/` 下装成 **system unit**（`systemctl` 不带 `--user`），
#   `%h` 会变成 `User=` 指定的 home，**没写 User= 就是 /root** —— 所有路径全指错，
#   而 systemd 本身**不会报错**：`systemd-analyze verify` 也只查语法、查不出这个。
#
#   所以本脚本装完后逐个 `systemctl show` 读**展开后的真实值**，再断言
#   ExecStart 里的可执行文件与脚本文件**确实存在**。装错会当场红，而不是等
#   第一次重启时才发现「服务起不来但看不出为什么」。
#
# 【装哪些】
#   meeting-web      必须（Web 面板）
#   meeting-watcher  必须（收件箱监听）
#   meeting-retention + .timer  默认装（只清音频，不碰文本）
#   meeting-tts      仅当 $TTS_DIR/venv/bin/python 存在（TTS 是独立 venv）
#
# 【用法】
#   bash deploy/install-units.sh              # 装 + reload + 断言（不 enable）
#   bash deploy/install-units.sh --enable     # 额外 enable（开机自启）
#   bash deploy/install-units.sh --dry-run    # 只打印要做什么
#   bash deploy/install-units.sh --all        # 忽略可选判定，全装
#
# 【退出码】0 成功 / 1 断言失败（已装但有 unit 展不开或文件不存在）/ 2 前置不满足
# ============================================================================ #
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/app}"
TTS_DIR="${TTS_DIR:-$HOME/tts}"
UNIT_DIR="${UNIT_DIR:-$HOME/.config/systemd/user}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

DRY_RUN=0
DO_ENABLE=0
FORCE_ALL=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --enable)  DO_ENABLE=1 ;;
    --all)     FORCE_ALL=1 ;;
    -h|--help) sed -n '3,29p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数：$arg（--help 看用法）" >&2; exit 2 ;;
  esac
done

log() { echo "[units] $*"; }
warn() { echo "[units] ⚠️  $*" >&2; }
die() { echo "[units] ❌ $*" >&2; exit "${2:-1}"; }

# --------------------------------------------------------------------------- #
# 前置：必须是 user 上下文
#   装成 system unit 时 `%h` 语义会变（见头部说明），所以这里直接拦住。
# --------------------------------------------------------------------------- #
if [ "${EUID:-$(id -u)}" -eq 0 ]; then
  die "不要用 root 跑本脚本。这些 unit 用的是 %h（当前用户 home），
       必须装成 user unit：以普通用户执行，并用 systemctl --user 管理。
       用 root 装到 /etc/systemd/system/ 会让 %h 指向 /root，所有路径全错。" 2
fi

[ -f "$SRC_DIR/meeting-web.service" ] || die "找不到 $SRC_DIR/meeting-web.service（要在仓库的 deploy/ 下跑）" 2

# --------------------------------------------------------------------------- #
# 选装清单
# --------------------------------------------------------------------------- #
# 元素是**完整文件名**（含后缀）：cp 直接用得上，也能原样打印给用户。
UNITS=(meeting-web.service meeting-watcher.service meeting-retention.service)
TIMERS=(meeting-retention.timer)

if [ "$FORCE_ALL" = 1 ] || [ -x "$TTS_DIR/venv/bin/python" ]; then
  UNITS+=(meeting-tts.service)
else
  warn "跳过 meeting-tts：$TTS_DIR/venv/bin/python 不存在（没装 TTS 就没有声音，其余功能正常）"
  warn "  要装：见 docs/声纹库与语音分身.md；或用 --all 强制装"
fi


log "将安装 ${#UNITS[@]} 个 service + ${#TIMERS[@]} 个 timer → $UNIT_DIR"
for u in "${UNITS[@]}" "${TIMERS[@]}"; do echo "        - $u"; done

if [ "$DRY_RUN" = 1 ]; then
  log "dry-run 结束，未写任何文件"
  exit 0
fi

# --------------------------------------------------------------------------- #
# 备份已有 unit（改坏了能回去）
# --------------------------------------------------------------------------- #
BACKUP="$HOME/unit-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
n_backup=0
for u in "${UNITS[@]}" "${TIMERS[@]}"; do
  if [ -f "$UNIT_DIR/$u" ] && [ -f "$SRC_DIR/$u" ]; then
    if ! cmp -s "$SRC_DIR/$u" "$UNIT_DIR/$u"; then
      cp "$UNIT_DIR/$u" "$BACKUP/"
      n_backup=$((n_backup + 1))
    fi
  fi
done
if [ "$n_backup" -gt 0 ]; then
  log "已备份 $n_backup 个将被覆盖的 unit → $BACKUP"
else
  rmdir "$BACKUP" 2>/dev/null || true
fi

# --------------------------------------------------------------------------- #
# 安装
#   数组里存的是**完整文件名**（含 .service/.timer），不是 unit 短名 ——
#   否则 cp 会去找 `deploy/meeting-web` 这种不存在的路径。
# --------------------------------------------------------------------------- #
mkdir -p "$UNIT_DIR"
for f in "${UNITS[@]}" "${TIMERS[@]}"; do
  cp "$SRC_DIR/$f" "$UNIT_DIR/$f"
done
systemctl --user daemon-reload || die "daemon-reload 失败"
log "已安装并 daemon-reload"

# --------------------------------------------------------------------------- #
# 断言：逐个读**展开后**的值
#    这是本脚本存在的理由 —— 只 cp 不验，%h 指错了谁也看不出来。
# --------------------------------------------------------------------------- #
fail=0
for u in "${UNITS[@]}"; do
  short="${u%.service}"          # systemctl 认短名（带 .service 也认，但短名更稳）
  unit_ok=1                      # 每个 unit 一个局部标志：绝不在报错后又打 ✅
  wd=$(systemctl --user show "$short" -p WorkingDirectory --value 2>/dev/null || true)
  es=$(systemctl --user show "$short" -p ExecStart --value 2>/dev/null || true)

  if [ -z "$wd" ]; then
    warn "$u：WorkingDirectory 为空（unit 没被 systemd 读到？）"; fail=1; continue
  fi
  # 用 case 而不是 ${wd#%h}：后者只在 %h 位于开头时有效，换个位置就漏检
  case "$wd" in
    *%h*) warn "$u：WorkingDirectory 仍是未展开的 $wd（%h 没生效，见头部说明）"; fail=1; continue ;;
  esac
  case "$es" in
    *%h*) warn "$u：ExecStart 里有未展开的 %h：$es"; fail=1; continue ;;
  esac
  # 🔴 真正防「装成 system unit」的那一条：
  #   %h 在 --user unit 里 = 当前用户 home；在 system unit 里 = User= 的 home，
  #   没写 User= 就是 /root。那种情况下 %h 是**会被正常展开**的（展开成 /root），
  #   所以「含未展开 %h」这个判据抓不到 —— 必须比对 wd 是否落在 $HOME 下。
  case "$wd" in
    "$HOME"|"$HOME"/*) : ;;
    *) warn "$u：WorkingDirectory=$wd 不在当前用户 home（$HOME）下。
       很可能被装成了 system unit（%h 会指向 /root）。请用 systemctl --user 管理。"
       fail=1; unit_ok=0 ;;
  esac
  if [ ! -d "$wd" ]; then
    warn "$u：WorkingDirectory=$wd 不存在"; fail=1; unit_ok=0
  fi

  # 从 ExecStart 里抽出可执行文件与脚本两个路径，逐个查存在性。
  # 形如：{ path=/usr/bin/python3 ; argv[]=/usr/bin/python3 %h/app/server.py ; ... }（%h 已被 systemd 换掉）
  exe=$(printf '%s' "$es" | sed -n 's/.*path=\([^ ;]*\).*/\1/p')
  script=$(printf '%s' "$es" | sed -n 's/.*argv\[\]=[^ ]* \([^ ;]*\).*/\1/p')

  if [ -z "$exe" ] || [ -z "$script" ]; then
    warn "$u：ExecStart 解析不出可执行文件/脚本（单命令可执行文件，如 oneshot 只给命令时正常）"
    unit_ok=0
  else
    if [ ! -x "$exe" ]; then
      warn "$u：ExecStart 的解释器不存在或不可执行：$exe"; fail=1; unit_ok=0
    fi
    if [ ! -f "$script" ]; then
      warn "$u：ExecStart 的脚本不存在：$script"; fail=1; unit_ok=0
    fi
  fi

  if [ "$unit_ok" = 1 ]; then
    log "✅ $u  wd=$wd  exe=$exe  script=$script"
  else
    log "❌ $u 未通过（见上面的具体原因）"
  fi
done

# --------------------------------------------------------------------------- #
# enable（可选）
# --------------------------------------------------------------------------- #
if [ "$DO_ENABLE" = 1 ]; then
  for u in "${UNITS[@]}"; do
    systemctl --user enable "$u" >/dev/null 2>&1 && log "已 enable $u" || warn "enable $u 失败"
  done
  for t in "${TIMERS[@]}"; do
    systemctl --user enable --now "$t" >/dev/null 2>&1 && log "已 enable+start $t" || warn "enable $t 失败"
  done
else
  log "未 enable（要开机自启：bash deploy/install-units.sh --enable）"
fi

# --------------------------------------------------------------------------- #
# 收尾
# --------------------------------------------------------------------------- #
if [ "$fail" != 0 ]; then
  die "有 unit 没通过展开断言。上面每条都写了具体哪个路径有问题。
       若是路径确实不存在（比如移动了代码目录），先改 .env 的 APP_DIR / 目录配置，
       再重跑本脚本。旧 unit 备份在 ${BACKUP:-（本次无覆盖）}。"
fi

short_list=""; for u in "${UNITS[@]}"; do short_list+="${u%.service} "; done
log "全部通过。启动：systemctl --user start $short_list"
if [ "${#TIMERS[@]}" -gt 0 ]; then
  log "定时任务由 timer 驱动：systemctl --user list-timers | grep meeting"
fi
exit 0
