#!/usr/bin/env bash
# ==============================================================================
# tools/check-style.sh —— 零依赖风格门禁
# ------------------------------------------------------------------------------
# 为什么不用「只靠 ruff」：本仓的提交发生在 2 类机器上（Windows 编辑机 / Linux 服务器），
# ruff 与 shfmt 不保证到处都有。**门禁如果依赖一个没装的工具，它就不是门禁，是一句愿望。**
#
# 设计：三层，缺哪层就明确说「未校验」，绝不静默放行（不制造假绿）。
#   ① 零依赖层（永远执行）：CRLF / 行尾空白 / Python 前导 Tab / 缺 final newline
#   ② ruff 层（有则执行）：ruff check（规则全部由 ruff.toml 决定：select=E,F,I + ignore=E501）
#                            ⚠️ 命令行不要再写 --select：它会覆盖 ruff.toml 里的 ignore，
#                            曾实测多报 109 处 E501（本仓 ignore E501 是有意的，见 lint 段注释）
#   ③ format 层（默认关闭）：需显式 --format，存量项目一次性重排见 ADR-0002
#
# 用法：
#   bash tools/check-style.sh <file>...     # 传文件列表（hook 会传 staged 文件）
#   bash tools/check-style.sh               # 不传 = 检查 git 已跟踪的文件（跳过 knowledge/legal）
#   bash tools/check-style.sh --format      # 额外检查 ruff format（全仓会报大量差异）
#   bash tools/check-style.sh --all         # 含 knowledge/ 全量（慢，仅排查用）
#
# 退出码：0 通过（或仅「未校验」）/ 1 有违规 / 2 环境错误
# ==============================================================================
set -uo pipefail

cd "$(git rev-parse --show-toplevel 2>/dev/null || echo .)" || exit 2

CHECK_FORMAT=0
INCLUDE_ALL=0
args=()
for a in "$@"; do
  case "$a" in
    --format) CHECK_FORMAT=1 ;;
    --all) INCLUDE_ALL=1 ;;
    *) args+=("$a") ;;
  esac
done

fail=0
note() { printf '  ✖ %s\n' "$*" >&2; fail=1; }
skip() { printf '  · 未校验：%s\n' "$*" >&2; }

# ---------- 收集待检文件 ----------
# 🔴 必须 -z + core.quotePath=false：`git ls-files` 默认把非 ASCII 路径输出成
#    带引号的八进制转义（"docs/APK-\346\211\223..."），后面 `[ -f "$f" ]` 全部落空
#    → 中文名文件被静默跳过。实测本仓 657 个跟踪文件里只有 122 个能命中，
#    535 个（含全部 docs/*.md）从来没被门禁看过。这就是假绿，必须堵死。
if [ ${#args[@]} -gt 0 ]; then
  files=("${args[@]}")
else
  mapfile -d '' -t files < <(git -c core.quotePath=false ls-files -z)
  if [ "$INCLUDE_ALL" -eq 0 ]; then
    # knowledge/legal 是外部抓取的法律原文（527 个文件），保持原样不参与门禁
    _kept=()
    for _f in "${files[@]}"; do
      case "$_f" in knowledge/*) ;; *) _kept+=("$_f") ;; esac
    done
    files=("${_kept[@]}")
  fi
fi

py=(); sh=(); _gone=0
for f in "${files[@]}"; do
  [ -f "$f" ] || { _gone=$((_gone + 1)); continue; }
  case "$f" in
    *.py) py+=("$f") ;;
    *.sh) sh+=("$f") ;;
  esac
done

echo "[check-style] 检查 ${#files[@]} 个文件（py=${#py[@]} sh=${#sh[@]}）"

# 清单里有、磁盘上没有：正常情况是 hook 把「本次删除的文件」也传了进来，少量属正常。
# 但如果一大半都落空 → 说明收集环节又坏了（历史上就是中文名被 git 转义那次），
# 那时门禁会一声不响地只检查少部分文件，是典型的假绿。所以过半直接判失败。
if [ "$_gone" -gt 0 ]; then
  printf '  · 清单里 %d 个路径在磁盘上不存在（已跳过）\n' "$_gone" >&2
  if [ ${#files[@]} -ge 20 ] && [ $((_gone * 2)) -gt ${#files[@]} ]; then
    note "超过半数路径取不到（$_gone/${#files[@]}）——文件清单收集大概率已失效，本次检查结果不可信"
  fi
fi

# ---------- ① 零依赖层 ----------
# 🔴 每一层都必须「一次调用扫一批文件」，不要 for 循环里逐文件起子进程：
#    Git Bash 起一个进程约 0.7s，原先每文件 3~4 次子进程 → 131 个文件的全仓自查
#    要跑十几分钟（实测 20 个文件 43s）。批量后整层固定在 5 次左右的进程开销，
#    全仓从 ~12 分钟降到秒级。**逐文件的写法看起来更直白，但它把门禁变成了摆设。**

# 待检文本文件：排除二进制 / 生成物 / 快照（这些进了 CRLF、末尾换行检查只会误报）
text_files=()
for f in "${files[@]}"; do
  [ -f "$f" ] || continue
  case "$f" in
    *.png|*.jpg|*.jpeg|*.gif|*.ico|*.webp|*.mp4|*.wav|*.mp3|*.apk|*.jar|*.zip|*.gz|*.tgz|*.pdf|*.whl|*.jks) continue ;;
  esac
  text_files+=("$f")
done

# 🔴 控制字符（CR / TAB）必须先落到变量，再在 <( ) 里用 "$VAR" 引用。
#    本机 MSYS bash 5.3.15 有个坑：`$'\r'` 这类 ANSI-C 引号直接写在进程替换**内部**会展开成
#    空字符串（顶层和 ( ) 子 shell 都正常）。空模式匹配任意行 → 每个文件都被报成含 CRLF，
#    而 `$'^ +\t|^\t'` 丢掉 TAB 后前导 Tab 检查直接失效。2026-10-20 实测，变量代入正常。
#    不要图省事把 $'\r' 写回 <( ) 里面。
CR=$'\r'
TAB_RE=$'^ +\t|^\t'

# ①-1 CRLF
#   用 grep -lU 而不是 tr：-U（--binary）关掉文本模式转换，MSYS 下不加它 CR 会被
#   当成行尾吃掉，pattern 永远匹配不到（老版本这里用 tr 逐文件数 CR 字节就是为了绕开，
#   代价是每文件两个进程）。不支持 -U 的 grep 退回 tr，慢但正确。
_grep_has_u() { grep -U -e '' /dev/null >/dev/null 2>&1; [ $? -ne 2 ]; }
if [ ${#text_files[@]} -gt 0 ]; then
  if _grep_has_u; then
    while IFS= read -r f; do
      note "$f: 含 CRLF（本仓行尾必须是 LF；修法：dos2unix $f 或用 newline='\\n' 重写）"
    done < <(grep -lU -e "$CR" -- "${text_files[@]}" 2>/dev/null)
  else
    for f in "${text_files[@]}"; do
      if [ "$(tr -cd '\r' < "$f" | wc -c | tr -d ' ')" != "0" ]; then
        note "$f: 含 CRLF（本仓行尾必须是 LF；修法：dos2unix $f 或用 newline='\\n' 重写）"
      fi
    done
  fi
fi

# ①-2 行尾空白（md 允许硬换行，只查其余类型；每文件最多报 5 行，避免刷屏）
nws_files=()
for f in "${text_files[@]}"; do
  case "$f" in *.md) ;; *) nws_files+=("$f") ;; esac
done
if [ ${#nws_files[@]} -gt 0 ]; then
  # grep -nH 强制带文件名前缀；awk 只留每文件前 5 条
  while IFS= read -r line; do note "$line"; done < <(
    grep -nHE '[[:space:]]+$' -- "${nws_files[@]}" 2>/dev/null \
      | awk -F: '{ c[$1]++; if (c[$1] <= 5) print $0 " 行尾空白" }'
  )
fi

# ①-3 Python 前导 Tab（混缩进会 SyntaxError 或缩进漂移）
if [ ${#py[@]} -gt 0 ]; then
  while IFS= read -r line; do note "$line"; done < <(
    grep -nHE "$TAB_RE" -- "${py[@]}" 2>/dev/null \
      | awk -F: '{ c[$1]++; if (c[$1] <= 5) print $0 " 前导 Tab" }'
  )
fi

# ①-4 final newline（只查文本类）
#   ⚠️ 不能用 `tail -c 1 | od` 逐文件判：每个文件两个进程，131 个文件就是 3 分钟。
#   等价判据：`grep -c '^'` 把「没有换行符的末行」也算一行，`wc -l` 只数换行符个数，
#   两者不等 ⟺ 末行缺换行。两次调用判完全部文件。
#   ⚠️ 只传 1 个文件时 wc / grep 都只打印数字、不打印文件名，解析会全部落空 → 静默漏报。
#      所以开头垫一个 /dev/null 当「哨兵」，强制两者都进入「带文件名」的输出格式。
nl_files=()
for f in "${text_files[@]}"; do
  case "$f" in
    *.py|*.sh|*.md|*.yaml|*.yml|*.json|*.toml|*.cfg|*.txt|*.js|*.css|*.html)
      [ -s "$f" ] && nl_files+=("$f") ;;
  esac
done
if [ ${#nl_files[@]} -gt 0 ]; then
  while IFS= read -r f; do note "$f: 缺文件末尾换行"; done < <(
    awk '
      FNR==NR { if ($1 == "total") next; n=$1; $1=""; sub(/^ /, ""); wc[$0]=n+0; next }
      # 从**行尾**认「:数字」，不要用 index($0,":") 取第一个冒号：Windows 绝对路径
      # （E:/a/b.py:12）第一个冒号在盘符后，会切出 "E" 当文件名，然后静默漏报。
      { if (!match($0, /:[0-9]+$/)) next
        f=substr($0, 1, RSTART-1); n=substr($0, RSTART+1)+0
        if (f in wc && wc[f] != n) print f }
    ' <(wc -l /dev/null "${nl_files[@]}" 2>/dev/null) \
      <(grep -c '^' /dev/null "${nl_files[@]}" 2>/dev/null)
  )
fi

# ①-5 个人绝对路径（P2-8）
#   为什么必须挡：`/home/<某人>`、`/Users/<某人>`、`C:\Users\<某人>` 既泄露用户名，
#   又让别人的机器上必然跑不通（路径不存在）。这类字符串**不会让任何测试失败**，
#   只会让仓库带着一个人的目录结构发出去 —— 所以只能靠门禁。
#
#   放行：占位符与文档假名
#     /home/xxx  /home/user  /home/<user>  /Users/you  /home/alice  /home/bob
#   报出：
#     /home/meeting  /home/ubuntu  /Users/john  C:\Users\someone  /root/
#
#   ⚠️ 豁免分两类，理由完全不同，别混在一起看：
#
#     ① **规则定义文件**：本文件、sanitize-check.py、tests/test_path_gate.py。
#        它们必须写出违规样式才能定义/测试规则，字面量是它们的数据。
#     ② **私有运维存档**：docs/仓库工作流-Gitee代码仓.md、docs/部署现状与迁移清单.md。
#        这两篇**不导出开源仓**（见 deploy/export-public.sh 的 EXCLUDE），
#        而且它们的主题**就是这台机器的布局** —— 去掉路径等于把内容删空。
#
#     🔴 ② 只对「路径」这一类成立，**不能推广**：
#        - 不能拿来豁免真名/IP（那是身份问题，与文件主题无关，由 sanitize-check 管，
#          而那边**零文件豁免**）；
#        - 若这两篇将来要发布，必须先删掉豁免、把路径换成占位符。
#     其他任何文件——包括新的测试——都不豁免。
#
#   ⚠️ 反斜杠只写两层（`\\{1,2}`）：grep -E 里 `\\` 就是「一个字面反斜杠」。
#      写成四层会变成「匹配两个反斜杠」，`C:\Users` 反而漏检 —— 第一版就这么写错了。
#      （2026-10-24 我自己在排查脚本里又踩了一次。）
PATH_GATE_SKIP_RE='^(tools/check-style\.sh|tools/sanitize-check\.py|tests/test_path_gate\.py|docs/仓库工作流-Gitee代码仓\.md|docs/部署现状与迁移清单\.md):'
ABS_RE='(/home/[a-z_][a-z0-9_-]*|/Users/[A-Za-z][A-Za-z0-9_.-]*|[A-Za-z]:\\{1,2}Users\\{1,2}[A-Za-z][A-Za-z0-9_.-]*|[A-Za-z]:\\{1,2}[^\\/:*?"<>|[:space:]]{2,40}|/root/)'
# 白名单：占位符 + RFC 惯用假名（alice/bob 不是真人）+ 通用系统目录
#
# 🔴 第二段 `[A-Za-z]:\\{1,2}[A-Za-z0-9_.一-龥-]{2,40}` 是 2026-10-24 补的。
#    原来只认 `C:\Users\<name>`，于是 `E:\work\...`、`D:\tools\...` 这类
#    **非用户目录的盘符路径全部漏检**。
#    新规则只匹配到第一级目录（`E:\work`），所以 `C:\Users` 这一级会单独出现，
#    必须写进白名单；真正的用户名由前一条规则抓。
ABS_OK_RE='/home/(xxx|user|username|yourname|alice|bob|USER|<)|/Users/(xxx|user|you|alice|bob|USER|<)|[A-Za-z]:\\{1,2}Users\\{1,2}(xxx|user|alice|bob|USER|<)|[A-Za-z]:\\{1,2}(Users|\.\.\.|projects|Windows|Program[ ]Files|Temp|tmp)$'
if [ ${#text_files[@]} -gt 0 ]; then
  while IFS= read -r line; do
    # line 形如 "path:12:/home/meeting/app"
    _f=${line%%:*}; _rest=${line#*:}; _ln=${_rest%%:*}; _val=${_rest#*:}
    # 🔴 必须**逐个匹配**判白名单，不能整行判：
    #    一行里同时出现 `/home/xxx`（占位）和 `/home/someone`（真名）时，
    #    整行判会让白名单把真名一起放过去 —— 2026-10-24 实测踩到，
    #    症状是「混在一行就漏报」，这类漏报比误报危险得多。
    _hit=""
    # 🔴 用 bash 内建 `=~` 判白名单，**不要** `printf | grep -qE`：
    #    每命中一个片段就 fork 一个 grep，实测 6 个小文件要 25 秒（sys 17s，几乎全是 fork）。
    #    改成内建匹配后降到 1 秒内。扫描本身不慢，慢在「逐个 spawn 进程」。
    while IFS= read -r _m; do
      [ -z "$_m" ] && continue
      [[ "$_m" =~ $ABS_OK_RE ]] && continue
      _hit="$_m"; break
    done < <(printf '%s' "$_val" | grep -oE "$ABS_RE" 2>/dev/null || true)
    [ -z "$_hit" ] && continue
    note "$_f:$_ln:$_val  ← 个人绝对路径（命中 $_hit；改成 %h / ~ / <user> 占位）"
  done < <(
    grep -nHE "$ABS_RE" -- "${text_files[@]}" 2>/dev/null \
      | grep -vE "$PATH_GATE_SKIP_RE" 2>/dev/null || true
  )
fi

# ---------- ② ruff lint 层 ----------
if [ ${#py[@]} -gt 0 ]; then
  if command -v ruff >/dev/null 2>&1; then
    if ! ruff check --no-fix "${py[@]}" >/tmp/.check_style_ruff_lint 2>&1; then
      echo "  ✖ ruff check 失败：" >&2
      sed -n '1,20p' /tmp/.check_style_ruff_lint >&2
      fail=1
    fi
  else
    skip "ruff lint（未安装 → E/F/I 未执行；装法：pipx install ruff）
          ⚠️ 本次提交的 import 顺序与未定义名**没有被检查**"
  fi
fi

# ---------- ③ format 层（默认关闭）----------
if [ "$CHECK_FORMAT" -eq 1 ] && [ ${#py[@]} -gt 0 ]; then
  if command -v ruff >/dev/null 2>&1; then
    if ! ruff format --check "${py[@]}" >/tmp/.check_style_ruff_fmt 2>&1; then
      echo "  ✖ ruff format 需要重排（一次性重排必须单独一个提交，见 ADR-0002）：" >&2
      sed -n '1,10p' /tmp/.check_style_ruff_fmt >&2
      fail=1
    fi
  else
    skip "ruff format（未安装）"
  fi
elif [ "$CHECK_FORMAT" -eq 0 ]; then
  skip "ruff format（默认关闭；存量 15K 行一次性重排会毁 blame，见 docs/adr/0002）"
fi

# ---------- shfmt 层 ----------
if [ ${#sh[@]} -gt 0 ]; then
  if command -v shfmt >/dev/null 2>&1; then
    if ! shfmt -d -i 2 "${sh[@]}" >/tmp/.check_style_shfmt 2>&1; then
      echo "  ✖ shfmt 需要重排（shfmt -i 2 -w <file>）：" >&2
      sed -n '1,15p' /tmp/.check_style_shfmt >&2
      fail=1
    fi
  else
    skip "shfmt（未安装 → Shell 缩进未执行）"
  fi
fi

if [ "$fail" -eq 0 ]; then
  echo "[check-style] ✅ 通过"
else
  echo "[check-style] ❌ 有违规，请修好后重新提交（可用 git commit --no-verify 绕过，但必须在提交说明里写明原因）" >&2
fi
exit "$fail"
