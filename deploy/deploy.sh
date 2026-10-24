#!/usr/bin/env bash
# ==============================================================================
# deploy/deploy.sh —— 服务器侧部署（六步 + 断言）
#
# 【为什么写这个脚本】2026-10-22 同一个坑踩了 3 次：
#   `git pull` 失败，但输出被 `| tail -N` 截掉 → 误判「部署成功」→ 上线后才发现没生效。
#   **断言比看输出可靠。** 本脚本每一步都用 test / grep / python 判定，
#   不依赖解析人类可读输出，也不允许任何一步「看起来成功就算过」。
#
# 【在哪跑】服务器（<你的主机>，账户 meeting），通常在 ~/app 内。
#   这不是本地脚本：它要 .venv/bin/python、systemctl --user、localhost:8848。
#
# 【用法】
#   bash deploy/deploy.sh
#       完整部署：pull → 断言 → 装配 → 重启 → health
#
#   bash deploy/deploy.sh --dry-run
#       只走到「装配」为止，不重启。想先确认能不能安全上线时用。
#
#   bash deploy/deploy.sh --expect '智能语音分身助理:web/index.html'
#       额外断言「本次改动确实到位」。格式 `串:文件`，可重复给多次。
#       只改一行的改动最容易出现「pull 成功但改的不是这个文件」，这条就是防它的。
#
#   bash deploy/deploy.sh --require-model
#       health 里必须有 model_loaded:true，超时即失败（默认超时只警告）。
#
#   bash deploy/deploy.sh --no-wait-model
#       不等模型加载（默认最多等 --model-timeout 秒）。
#
#   bash deploy/deploy.sh --force
#       有人正在录音也强制重启（会打断别人的会话，慎用）。
#
#   UNITS=web,watcher,tts bash deploy/deploy.sh
#       手动指定重启哪些 unit，跳过 diff 判定（应急用；默认按改动文件自动判定）。
#       背景：watcher / tts 是独立进程，改 transcribe.py / config.py 后只重启 web
#       会让它们继续跑旧代码，且不报错。自动判定表见「步骤 4.5」。
#
# 【退出码】
#   0 成功 / 1 断言失败（pull、装配、关键串、health）/ 2 前置条件不满足（脏工作区、正在录音）
#
# 【为什么不 scp】scp 一个文件进 ~/app 会变成未跟踪/已修改，把下一次 `git pull` 挡死。
#   临时脚本一律放 /tmp/。本脚本自己就该通过 git 分发，不要手工拷。
#
# 【🔴 本脚本会更新它自己，所以改了它就要跑两次】
#   `git pull` 会把 deploy/deploy.sh 换成新版，但**正在运行的那个 bash 已经按字节偏移
#   在读了**：换文件之后它继续从旧偏移读，读到的是旧尾部（或半截），于是新加的收尾逻辑
#   （写 .deploy-state、打印"已记录部署基准"）**静默不执行**，且退出码仍是 0。
#   2026-10-23 一天内踩了两次：
#     ① 第一次 dry-run 看不到"重启范围"一行（那段是新加的）；
#     ② 正式部署后 .deploy-state 没生成，导致下次又算错变更。
#   判断方法：拉完后比一下 `git rev-parse HEAD` 与本次实际生效的逻辑对不对得上；
#   稳妥做法就是**改了 deploy.sh 的提交，部署时连跑两次**。
# ==============================================================================
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/app}"
BRANCH="${BRANCH:-origin/master}"
WEB_UNIT="${WEB_UNIT:-meeting-web}"
WATCHER_UNIT="${WATCHER_UNIT:-meeting-watcher}"
TTS_UNIT="${TTS_UNIT:-meeting-tts}"
PORT="${PORT:-8848}"
PY="${PY:-.venv/bin/python}"

DRY_RUN=0
FORCE=0
ALLOW_DIRTY=0
REQUIRE_MODEL=0
WAIT_MODEL=1
MODEL_TIMEOUT=45
EXPECT=()

log()  { printf '[deploy] %s\n' "$*"; }
ok()   { printf '[deploy] ✅ %s\n' "$*"; }
warn() { printf '[deploy] ⚠️  %s\n' "$*" >&2; }
die()  { local msg="$1" code="${2:-1}"; printf '[deploy] ❌ %s\n' "$msg" >&2; exit "$code"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)        DRY_RUN=1 ;;
    --force)          FORCE=1 ;;
    --allow-dirty)    ALLOW_DIRTY=1 ;;
    --require-model)  REQUIRE_MODEL=1 ;;
    --no-wait-model)  WAIT_MODEL=0 ;;
    --model-timeout)  shift; MODEL_TIMEOUT="${1:?--model-timeout 需要一个秒数}" ;;
    --expect)         shift; EXPECT+=("${1:?--expect 需要 串:文件}") ;;
    -h|--help)        sed -n '2,53p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)                die "不认识的参数：$1（--help 看用法）" 2 ;;
  esac
  shift
done

cd "$APP_DIR" 2>/dev/null || die "进不去 $APP_DIR（用 APP_DIR=... 指定）" 2

# ============================================================================ #
# 步骤 0a：工作区必须干净
#   不干净就别 pull —— 尤其是「未跟踪文件」，那通常是 scp 塞进来的，
#   它会在 pull 时挡住一切，而错误信息很容易被 | tail 截掉看不见。
# ============================================================================ #
DIRTY=$(git status --porcelain | wc -l | tr -d ' ')
if [ "$DIRTY" != "0" ]; then
  if [ "$ALLOW_DIRTY" = "1" ]; then
    warn "工作区有 $DIRTY 处改动（--allow-dirty 放行，后果自负）"
    git status --short
  else
    git status --short >&2
    die "工作区不干净（$DIRTY 处）。典型原因：scp 塞进来的文件挡住了 pull。临时脚本请放 /tmp/。" 2
  fi
fi

BEFORE=$(git rev-parse --short HEAD)
log "部署前版本：$BEFORE（VERSION=$(cat VERSION 2>/dev/null || echo '?')）"

# ============================================================================ #
# 步骤 0b：有人在录音就别重启（**只在真要重启时检查**，定义见下）
#   restart 会把正在进行的实时会话掐掉。这条以前是口头约定。
#   注意：判断时机在 DRY_RUN 之后，见 check_sessions 的注释。
# ============================================================================ #
check_sessions() {
  local sessions=0 body
  if body=$(curl -s --max-time 5 "localhost:$PORT/api/health" 2>/dev/null) && [ -n "$body" ]; then
    sessions=$("$PY" -c 'import json,sys;print(json.loads(sys.argv[1]).get("sessions",0))' "$body" 2>/dev/null || echo 0)
  fi
  if [ "$sessions" != "0" ]; then
    if [ "$FORCE" = "1" ]; then
      warn "当前有 $sessions 个会话在跑，--force 强制继续"
    else
      die "当前有 $sessions 个会话在跑，拒绝重启（确认没人在录再加 --force）" 2
    fi
  fi
}

# ============================================================================ #
# 步骤 1：pull
#   失败信息单独打出来，不让它被任何人用管道截掉。
# ============================================================================ #
log "git pull --ff-only"
if ! PULL_OUT=$(git pull --ff-only 2>&1); then
  printf '%s\n' "$PULL_OUT" >&2
  die "git pull 失败。若是 'untracked working tree files would be overwritten'，
      说明有文件是 scp 进来的 —— 用 git status 看清后删掉，别手工覆盖。" 1
fi
printf '%s\n' "$PULL_OUT" | tail -3

# ============================================================================ #
# 步骤 2：核心断言 —— HEAD 必须等于远端（七位）
#   这是整个脚本的心脏。pull 的输出可能被截断、可能谎报，
#   但 rev-parse 是事实。少了这条，今天就还会错一次。
# ============================================================================ #
HEAD_SHA=$(git rev-parse --short HEAD)
ORIG_SHA=$(git rev-parse --short "$BRANCH")
[ "$HEAD_SHA" = "$ORIG_SHA" ] || die "版本断言失败：HEAD=$HEAD_SHA 但 $BRANCH=$ORIG_SHA，pull 没到位" 1
VERSION_NOW=$(cat VERSION 2>/dev/null || echo '?')
ok "版本断言通过：$HEAD_SHA（VERSION=$VERSION_NOW）"

# ============================================================================ #
# 步骤 3：装配 —— import 得通、app 建得起来
#   放在 restart 之前：装配不过就停，旧进程还在跑，用户完全无感。
#   反过来说，装配通过不代表功能对，所以后面还要 health 与关键串。
# ============================================================================ #
log "装配检查：import server; server.create_app()"
if ! "$PY" -c 'import server; server.create_app()' 2>&1; then
  die "装配失败：server.py / api/ 有导入或建 app 错误。未重启，旧进程仍在服务。" 1
fi
ok "装配通过"

# ============================================================================ #
# 步骤 4：关键串（可选）—— 「真的到位了吗」
#   只改一行的改动，最容易出现「pull 成功、装配通过、但改的不是这个文件」。
# ============================================================================ #
for pair in ${EXPECT+"${EXPECT[@]}"}; do
  needle="${pair%%:*}"
  file="${pair##*:}"
  [ -f "$file" ] || die "--expect 指定的文件不存在：$file" 1
  grep -qF -- "$needle" "$file" || die "关键串没到位：'$needle' 不在 $file 里" 1
  ok "关键串到位：'$needle' @ $file"
done

# ============================================================================ #
# 步骤 4.5：算本次该重启哪些 unit（按 diff 判定，不靠人记）
#
#   为什么要有这一步：本脚本原来只重启 $WEB_UNIT。但 `meeting-watcher` 与
#   `meeting-tts` 是**独立进程**，各自在启动时 import 一份代码。改了
#   transcribe.py / config.py 之后只重启 web，watcher 仍在跑旧代码 ——
#   症状是「网页正常，但上传的文件转写行为不一样」，而且**没有任何报错**。
#   （2026-10-23 实测：`grep -c "meeting-watcher\|meeting-tts" deploy/deploy.sh` 原本是 0。）
#
#   判定逻辑在 deploy/plan_units.sh（独立成文件是为了能被测试直接 source，
#   判定表与依赖闭包的来由也写在那里面）。
#
#   可用 UNITS="web,watcher,tts" 强制指定，绕过判定（应急用）。
#
# 🔴 基准不是 BEFORE，而是「上次成功部署的 commit」（DEPLOY_STATE 文件）。
#   为什么：`--dry-run` 也会执行 git pull，于是
#       先 dry-run 再正式跑 → BEFORE 已经是新 HEAD → diff 为空
#       → 判定「无变更」→ 只重启 web → watcher/tts 继续跑旧代码。
#   2026-10-23 实测踩到：dry-run 报「重启 web+watcher+tts」，
#   紧接着正式跑却只重启了 web。用持久化基准才能正确表达
#   「自上次部署以来改了什么」，而且 dry-run 不写基准，天然幂等。
# ============================================================================ #
DEPLOY_STATE="$APP_DIR/.deploy-state"
# shellcheck source=plan_units.sh
. "$(dirname "$0")/plan_units.sh"

BASE_FOR_DIFF=$(resolve_diff_base "$BEFORE" "$DEPLOY_STATE")
if [ "$BASE_FOR_DIFF" != "$BEFORE" ]; then
  log "diff 基准取上次成功部署的 $BASE_FOR_DIFF（而非本次 pull 前的 $BEFORE）"
fi

CHANGED=$(git diff --name-only "$BASE_FOR_DIFF" "$HEAD_SHA" 2>/dev/null || true)
if [ -n "$CHANGED" ]; then
  log "本次变更 $BASE_FOR_DIFF..$HEAD_SHA 涉及 $(printf '%s\n' "$CHANGED" | wc -l | tr -d ' ') 个文件"
else
  log "自上次部署（$BASE_FOR_DIFF）以来无代码变更"
fi

if [ -n "${UNITS:-}" ]; then
  warn "UNITS=$UNITS 手动指定，跳过 diff 判定"
  need_watcher=0
  need_tts=0
  case ",$UNITS," in *,watcher,*) need_watcher=1 ;; esac
  case ",$UNITS," in *,tts,*)     need_tts=1 ;; esac
else
  plan_units "$CHANGED"
fi

# ⚠️ 这里必须用 if / 函数，不能写 `[ x ] && ...`：本脚本带 `set -e`，
#   `[ 假 ] && cmd` 会返回 1 让脚本直接退出（只改 web 文件时就会中止）。
RESTART_PLAN=$(restart_plan "$WEB_UNIT" "$WATCHER_UNIT" "$TTS_UNIT")
log "重启范围：$RESTART_PLAN"

if [ "$DRY_RUN" = "1" ]; then
  ok "dry-run 结束：pull / 断言 / 装配都过了，未重启（服务仍是 $BEFORE）"
  echo "[deploy] 若正式执行，将重启：$RESTART_PLAN"
  exit 0
fi

# ============================================================================ #
# 步骤 5：重启（先查有没有人在录）
#   改 server.py 必须重启：transcribe 是收尾阶段才 import，
#   不重启会出现「新代码一半、旧代码一半」的假象（曾因此误判 assistant_events 为空）。
#   ⚠️ 只改了 APK 资源 / 文档 / 测试时不必重启 —— 那种情况用 --dry-run 拉代码即可。
#   ⚠️ watcher / tts 不打断实时会话，但 watcher 重启会中断**正在处理的文件转写**
#      （pipeline 单进程串行），所以同样要提示。
# ============================================================================ #
check_sessions
log "systemctl --user restart $WEB_UNIT"
systemctl --user restart "$WEB_UNIT" || die "重启失败，看 journalctl --user -u $WEB_UNIT" 1

if [ "$need_watcher" = 1 ]; then
  log "systemctl --user restart $WATCHER_UNIT（本次改动涉及转写链路）"
  systemctl --user restart "$WATCHER_UNIT" \
    || die "重启 $WATCHER_UNIT 失败，看 journalctl --user -u $WATCHER_UNIT（web 已重启，watcher 仍是旧代码）" 1
fi
if [ "$need_tts" = 1 ]; then
  log "systemctl --user restart $TTS_UNIT（本次改动涉及配置或音色）"
  systemctl --user restart "$TTS_UNIT" \
    || die "重启 $TTS_UNIT 失败，看 journalctl --user -u $TTS_UNIT" 1
fi

# ============================================================================ #
# 步骤 6：health 断言
#   两个坑都在这里：
#   ① 只看 ok:true 不够 —— 进程活着不代表版本对，所以要断言 version 等于 VERSION 文件；
#   ② model_loaded 是**异步**的（--warmup 线程加载 1.2GB 模型，实测 3.8s），
#      重启后立刻查必然是 false。所以不能「查一次 false 就判失败」，
#      要轮询等待（默认 45s），超时只警告 —— 因为模型没加载完服务仍可用，
#      第一次点开始时 ensure_loaded() 会再试。
#      要严格就在验收场景加 --require-model。
# ============================================================================ #
check_health() {
  # 输出 OK:<模型是否加载 0/1>:<原始 json>，或 FAIL:<原因>
  # 注意：**不要用 grep 去匹配 JSON 的文本形态**。json.dumps 默认是
  # `"model_loaded": true`（冒号后带空格），而手写模式很容易写成 `"model_loaded":true`，
  # 结果永远匹配不上 → 每次部署都白等满超时，还打印出「已通过」与「仍为 false」自相矛盾的两行。
  # （2026-10-22 实测踩到，耗时 48s 才暴露。）
  "$PY" - "$1" "$VERSION_NOW" <<'PY'
import json, sys
raw, want_ver = sys.argv[1], sys.argv[2]
try:
    d = json.loads(raw)
except Exception:
    print("BAD"); raise SystemExit
if not d.get("ok"):
    print("FAIL:ok 不是 true"); raise SystemExit
if want_ver and want_ver != "?" and d.get("version") != want_ver:
    print(f"FAIL:version={d.get('version')} 与 VERSION={want_ver} 不一致"); raise SystemExit
flag = 1 if d.get("model_loaded") else 0
print(f"OK:{flag}:" + json.dumps(d, ensure_ascii=False))
PY
}

OLD_MODEL_LOADED=0
HEALTH_OK=0
for _ in $(seq 1 "$MODEL_TIMEOUT"); do
  BODY=$(curl -s --max-time 8 "localhost:$PORT/api/health" 2>/dev/null || true)
  if [ -n "$BODY" ]; then
    RES=$(check_health "$BODY" || true)
    case "$RES" in
      OK:*)
        HEALTH_OK=1
        REST="${RES#OK:}"
        MODEL_FLAG="${REST%%:*}"
        DETAIL="${REST#*:}"
        if [ "$MODEL_FLAG" = "1" ]; then OLD_MODEL_LOADED=1; fi
        if [ "$OLD_MODEL_LOADED" = "1" ] || [ "$WAIT_MODEL" = "0" ]; then break; fi
        ;;
      FAIL:*)
        die "health 断言失败：${RES#FAIL:}" 1
        ;;
    esac
  fi
  sleep 1
done

[ "$HEALTH_OK" = "1" ] || die "health 在 ${MODEL_TIMEOUT}s 内没起来，看 journalctl --user -u $WEB_UNIT -n 50" 1

if [ "$OLD_MODEL_LOADED" = "1" ]; then
  ok "health 通过（含模型已加载）：$DETAIL"
else
  if [ "$REQUIRE_MODEL" = "1" ]; then
    die "health 的 model_loaded 仍为 false（${MODEL_TIMEOUT}s 超时）。--require-model 要求必须为 true。" 1
  fi
  warn "health 的 ok/version 已通过，但 model_loaded 仍为 false（异步加载，可能还在跑）。"
  warn "这不阻塞服务：第一次点开始时 ensure_loaded() 会再试。要严格请加 --require-model。"
  ok "health 通过：$DETAIL"
fi

# 记录「已成功部署到哪个 commit」。下次判定重启范围以它为基准，
# 这样 --dry-run（会 pull 但不重启）不会把变更信息"吃掉"。
# 只在真正重启 + health 通过之后才写。
printf '%s\n' "$HEAD_SHA" > "$DEPLOY_STATE"

ok "部署完成：$BEFORE → $HEAD_SHA（VERSION=$VERSION_NOW）"
if [ "$BASE_FOR_DIFF" != "$HEAD_SHA" ]; then
  log "已记录部署基准：$HEAD_SHA（下次按它算变更）"
fi
exit 0
