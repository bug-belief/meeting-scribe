#!/usr/bin/env bash
# ==============================================================================
# plan_units.sh —— 判定「本次改动需要重启哪些 unit」
#
# 【为什么单独成文件】deploy.sh 原来只重启 meeting-web，改 transcribe.py /
#   config.py 之后 watcher 与 tts 仍在跑旧代码，症状是「网页正常，但上传文件
#   行为不一样」，且**没有任何报错**。这段判定是护栏，护栏必须有反向对照，
#   所以抽成纯函数，让 tests/test_deploy_units.py 能直接 source 并真跑。
#
# 【用法】
#   source deploy/plan_units.sh
#   plan_units "$CHANGED"            # CHANGED = git diff --name-only 的输出
#   echo "$need_watcher $need_tts"   # 1/0
#
# 【判定表怎么来的】2026-10-23 用脚本算的 import 传递闭包，不是猜的：
#   watcher.py ─┬→ config / pipeline / transcribe / utils
#               ├→ pipeline 再 → summarize / voice_corpus / schema_guard / costs
#               ├→ transcribe 再 → diarize / speakers
#               └→ summarize 再 → llm_errors / prompts
#   tts_server.py → config（第 467 行函数内 import）
#   只判定**仓库根**下的 .py；api/ web/ tools/ deploy/ 的改动由 web 承担。
#
# 【已知边界】新增根目录 .py 且被 watcher 依赖时，必须把它加进 WATCHER_DEPS，
#   否则漏重启。tests/test_deploy_units.py 有一条断言盯着「根目录每个 .py
#   是否都在某张表里」，漏加会被测出来（不是靠人记）。
# ==============================================================================

# watcher 的依赖闭包（= watcher.py 自己 + 它传递依赖的全部根模块）
# ⚠️ cloud_asr 是 2026-10-23 新增的：被 transcribe.py（CloudBackend）引用，
#    而 transcribe 在 watcher 闭包里 → 改它必须重启 watcher。
WATCHER_DEPS='^(cloud_asr|config|costs|diarize|llm_errors|pipeline|prompts|schema_guard|speakers|summarize|transcribe|utils|voice_corpus|watcher)\.py$'
# tts 的依赖闭包
TTS_DEPS='^(config|tts_server)\.py$'
# 根目录 .py 里「只影响 web」的（改这些不重启 watcher/tts）
WEB_ONLY_DEPS='^(assistant|bootstrap|cost_report|kb_bridge|kb_search|live_engine|prompter|redact|server|tools|voice_register|websearch)\.py$'

# 结果写到这两个全局变量（调用方读它们）
need_watcher=0
need_tts=0

plan_units() {
  local changed="${1:-}" f
  need_watcher=0
  need_tts=0
  while IFS= read -r f; do
    [ -z "$f" ] && continue
    case "$f" in */*) continue ;; esac          # 只看仓库根的文件
    # 用 bash 内建 =~ 而不是 `printf | grep -qE`：后者每次 fork 两个进程，
    # 在 Git Bash / Windows 上一次 fork 就要几百毫秒（实测 39 个样本耗时 39s）。
    if [[ $f =~ $WATCHER_DEPS ]]; then need_watcher=1; fi
    if [[ $f =~ $TTS_DEPS ]];     then need_tts=1; fi
  done <<< "$changed"
  return 0
}

# 决定「拿哪个 commit 当 diff 基准」。
#   用法：resolve_diff_base <fallback> <state-file>
#
# 🔴 为什么不能用「本次 pull 前的 HEAD」：`--dry-run` 也会 pull，
#   于是「先干跑再正式跑」时 fallback 已经是新 HEAD → diff 为空 →
#   判定无变更 → 只重启 web，watcher/tts 继续跑旧代码。
#   改成读「上次成功部署到的 commit」，dry-run 不写这个文件，天然幂等。
#
# 状态文件不存在 / 内容不是有效 commit（比如 force push 后 SHA 变了）→ 退回 fallback。
resolve_diff_base() {
  local fallback="$1" state="$2" last=""
  if [ -f "$state" ]; then
    last=$(tr -d '[:space:]' < "$state" 2>/dev/null || true)
    if [ -n "$last" ] && git cat-file -e "$last^{commit}" 2>/dev/null; then
      printf '%s' "$last"
      return 0
    fi
  fi
  printf '%s' "$fallback"
}

#   用法：restart_plan [web名] [watcher名] [tts名]   （缺省用真实 unit 名）
# ⚠️ 全用 if 而不是 `[ ... ] && ...`：deploy.sh 带 `set -e`，
#    `[ 假 ] && cmd` 作为一条 AND-list 会返回 1 → **脚本直接退出**。
#    这是 2026-10-23 差点上线的一个 bug（只改 web 文件时部署会莫名中止）。
# 把判定结果拼成一行可打印的重启清单。
#   用法：restart_plan [web名] [watcher名] [tts名]   （缺省用真实 unit 名）
restart_plan() {
  local web="${1:-meeting-web}"
  local watcher="${2:-meeting-watcher}"
  local tts="${3:-meeting-tts}"
  local plan="$web"
  if [ "${need_watcher:-0}" = 1 ]; then
    plan="$plan + $watcher"
  fi
  if [ "${need_tts:-0}" = 1 ]; then
    plan="$plan + $tts"
  fi
  printf '%s' "$plan"
}
