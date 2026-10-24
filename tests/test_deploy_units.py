#!/usr/bin/env python3
"""deploy/plan_units.sh 护栏：按改动文件判定该重启哪些 unit。

## 为什么要有这个测试

`deploy/deploy.sh` 原来只重启 `meeting-web`。但 `meeting-watcher` 与
`meeting-tts` 是独立进程，各自在启动时 import 一份代码 —— 改了
`transcribe.py` / `config.py` 之后只重启 web，它们仍在跑旧代码。

症状是**静默**的：「网页正常，但上传的文件转写行为不一样」，没有任何报错。
所以这段判定必须有测试盯着，否则下一个人改判定表时很容易把它改坏。

## 怎么测（不是抄副本）

**直接 `source` 真文件**（`deploy/plan_units.sh`）再调它的 `plan_units()`，
真正跑 bash —— 不是用 Python 模拟一遍逻辑。这样判定表改了，测试自动跟随。

## 反向对照

每类都造样本证明它**真的会**返回 1，也有负样本证明它**不会**误报。
另有一条「完整性断言」：根目录每个 `.py` 必须落在某张表里，
否则新增模块时漏归类会被立刻测出来（而不是等到线上出诡异现象）。

跑法：
    python tests/test_deploy_units.py
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAN = ROOT / "deploy" / "plan_units.sh"

TOTAL = 0
FAILED = 0


def check(desc: str, got, want) -> None:
    """断言。注意签名是 check(desc, got, want) —— 不是 check(desc, 布尔)。"""
    global TOTAL, FAILED
    TOTAL += 1
    if got == want:
        print(f"  ok   {desc}")
    else:
        FAILED += 1
        print(f"  FAIL {desc}\n         得={got!r} 期望={want!r}")


def plan_many(names: list[str]) -> dict[str, tuple[int, int]]:
    """批量算，**一次 bash 调用算完所有样本**。

    为什么不逐个起进程：每起一次 bash 约 2s（Windows 上 CreateProcess 更慢），
    52 条断言就是 100+ 秒，会把全量回归从 140s 拖到 230s。
    批量版把所有输入写进临时文件，bash 侧循环读，一次拿回全部结果。

    ⚠️ 输入走**临时文件**，既不走命令行参数也不走 stdin：
    - Windows 的 Git Bash 经 CreateProcess 转义后，含换行的 argv 会被截断成第一行
      （2026-10-23 实测：多文件场景全判成 0，看着像判定逻辑坏了，其实是传参坏了）；
    - 走 stdin 又容易和 `$(cat)` 互相等（实测挂住超时）。
    ⚠️ 临时文件必须在 **with 块结束之后**才让 bash 读：Windows 上 NamedTemporaryFile
       在打开期间持有独占锁，子进程会读不到。
    """
    if not names:
        return {}
    with tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8", newline="\n"
    ) as fh:
        # 用 \x1e（记录分隔符）切样本，避免与内容里的换行冲突
        fh.write("\x1e".join(names))
        path = fh.name
    try:
        # `|| [ -n "$item" ]` 是必需的：最后一个样本后面没有分隔符，
        # read -d 读到 EOF 会返回非 0，不补这一句就会丢掉最后一条。
        script = (
            f'. "{PLAN}"\n'
            f'while IFS= read -r -d $\'\\x1e\' item || [ -n "$item" ]; do\n'
            f'  plan_units "$item"\n'
            f'  printf "%s %s\\n" "$need_watcher" "$need_tts"\n'
            f'done < {shlex.quote(path)}\n'
        )
        proc = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=60,
        )
    finally:
        os.unlink(path)
    if proc.returncode != 0:
        raise RuntimeError(f"plan_units 跑失败：{proc.stderr[:300]}")
    lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip()]
    out = {}
    for name, ln in zip(names, lines):
        w, t = ln.split()
        out[name] = (int(w), int(t))
    if len(lines) != len(names):
        raise RuntimeError(f"结果条数不符：要 {len(names)} 得 {len(lines)}")
    return out


def main() -> int:
    if not PLAN.is_file():
        print(f"❌ 找不到 {PLAN}")
        return 1

    print("\n0) 前置：脚本存在且语法正确")
    for f in (PLAN, ROOT / "deploy" / "deploy.sh"):
        proc = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True,
                              timeout=30)
        check(f"{f.name} 语法检查通过", proc.returncode, 0)

    # 一次 bash 调用算完全部样本（逐个起进程会让本套件跑 115s）
    WATCHER_CLOSURE = ["watcher.py", "config.py", "pipeline.py", "transcribe.py",
                       "summarize.py", "diarize.py", "speakers.py", "voice_corpus.py",
                       "costs.py", "utils.py", "schema_guard.py", "prompts.py",
                       "llm_errors.py"]
    WEB_ONLY = ["server.py", "live_engine.py", "assistant.py", "prompter.py",
                "kb_search.py", "kb_bridge.py", "redact.py", "websearch.py",
                "cost_report.py", "voice_register.py", "tools.py"]
    NON_ROOT = ["docs/架构与选型.md", "web/app.js", "api/health.py",
                "tests/test_dual.py", "deploy/deploy.sh", "knowledge/legal/README.md",
                "README.md", "requirements.txt", ".gitignore"]
    MIXED = [
        ("", (0, 0)),
        ("docs/a.md\ntranscribe.py", (1, 0)),
        ("web/app.js\nconfig.py", (1, 1)),
        ("tts_server.py\ntranscribe.py", (1, 1)),
        ("server.py\nlive_engine.py\nprompter.py", (0, 0)),
    ]

    samples = WATCHER_CLOSURE + ["tts_server.py"] + WEB_ONLY + NON_ROOT + [m[0] for m in MIXED]
    results = plan_many(samples)

    print("\n1) 正向：watcher 闭包内的文件 → 必须重启 watcher")
    # 这 13 个是 watcher.py 的传递依赖闭包（2026-10-23 用脚本实算，见 plan_units.sh 注释）
    for name in WATCHER_CLOSURE:
        check(f"改 {name} → need_watcher=1", results[name][0], 1)

    print("\n2) 正向：config.py / tts_server.py → 必须重启 tts")
    for name in ["config.py", "tts_server.py"]:
        check(f"改 {name} → need_tts=1", results[name][1], 1)

    print("\n3) 负向：只影响 web 的文件 → 两个都不该重启")
    for name in WEB_ONLY:
        check(f"改 {name} → 不重启 watcher/tts", results[name], (0, 0))

    print("\n4) 负向：非根目录 / 非 .py 的改动 → 两个都不该重启")
    for path in NON_ROOT:
        check(f"改 {path} → 不重启 watcher/tts", results[path], (0, 0))

    print("\n5) 边界：空变更 / 多个文件混合")
    for changed, want in MIXED:
        label = repr(changed) if changed else "空变更"
        check(f"{label} → {want}", results[changed], want)

    print("\n6) 完整性：根目录每个 .py 都必须在某张表里（防新增模块漏归类）")
    # 从 plan_units.sh 里抽三张正则（不抄副本）
    src = PLAN.read_text(encoding="utf-8")
    import re
    tables = {}
    for key in ("WATCHER_DEPS", "TTS_DEPS", "WEB_ONLY_DEPS"):
        m = re.search(rf"^{key}='([^']+)'", src, re.M)
        check(f"{key} 在 plan_units.sh 里能解析到", bool(m), True)
        tables[key] = m.group(1) if m else ""

    root_py = sorted(p.name for p in ROOT.glob("*.py"))
    check("根目录 .py 数量 > 0（防路径写错导致空断言）", len(root_py) > 10, True)
    unclassified = [
        n for n in root_py
        if not re.search(tables["WATCHER_DEPS"], n)
        and not re.search(tables["TTS_DEPS"], n)
        and not re.search(tables["WEB_ONLY_DEPS"], n)
    ]
    check(f"无未归类的根模块（未归类：{unclassified}）", unclassified, [])

    print("\n7) 反向对照：故意破坏判定表，确认测试真的会红")
    # 做法：临时把 WATCHER_DEPS 改成匹配不到任何东西，transcribe.py 就该返回 0
    broken = src.replace(
        "WATCHER_DEPS='^(config|",
        "WATCHER_DEPS='^(ZZZ_NOMATCH|config|",
        1,
    )
    # 上面这招不改变行为（只是加了个永不命中的分支），所以换个思路：
    # 把整条正则替换成只匹配一个不存在的名字
    broken = re.sub(r"^WATCHER_DEPS='[^']+'", "WATCHER_DEPS='^(zzz_nothing_here)$'", src, flags=re.M)
    tmp = ROOT / "tests" / "_tmp_plan_units_broken.sh"
    try:
        tmp.write_text(broken, encoding="utf-8")
        out = subprocess.run(
            ["bash", "-c",
             f'. "{tmp}"; plan_units "transcribe.py"; printf "%s" "$need_watcher"'],
            capture_output=True, text=True, timeout=30,
        )
        check("破坏判定表后 transcribe.py 不再触发 watcher（证明测试非虚绿）",
              out.stdout.strip(), "0")
    finally:
        tmp.unlink(missing_ok=True)

    print("\n8) set -e 兼容性：全是假条件时也不能让脚本退出")
    # deploy.sh 带 `set -euo pipefail`。`[ 假 ] && cmd` 作为 AND-list 会返回 1
    # → 脚本直接退出（症状是「只改 web 文件时部署莫名中止」）。这里在 set -e 下真跑。
    for changed in ["", "docs/a.md", "server.py", "config.py"]:
        with tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8", newline="\n"
        ) as fh:
            fh.write(changed)
            path = fh.name
        try:
            script = (
                "set -euo pipefail\n"
                f'. "{PLAN}"\n'
                f'plan_units "$(cat {shlex.quote(path)})"\n'
                'RESTART_PLAN=$(restart_plan web watcher tts)\n'
                'printf "%s" "$RESTART_PLAN"\n'
                'echo "|REACHED_END"\n'
            )
            proc = subprocess.run(
                ["bash", "-c", script], capture_output=True, text=True, timeout=30,
            )
        finally:
            os.unlink(path)
        check(f"set -e 下 changed={changed!r} 跑到结尾且 rc=0",
              ("|REACHED_END" in proc.stdout, proc.returncode), (True, 0))

    print("\n9) resolve_diff_base：dry-run 不能把变更信息「吃掉」")
    # 2026-10-23 实测踩到的真 bug：--dry-run 也会 git pull，于是「先干跑再正式跑」
    # 时 BEFORE 已经是新 HEAD → diff 为空 → 只重启 web，watcher/tts 跑旧代码。
    # 修法是拿「上次成功部署的 commit」当基准，dry-run 不写这个文件。
    tmp_repo = Path(tempfile.mkdtemp(prefix="diffbase-"))
    try:
        subprocess.run(["git", "init", "-q", "-b", "master"], cwd=tmp_repo, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_repo, check=True)
        (tmp_repo / "a.txt").write_text("1\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=tmp_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c1"], cwd=tmp_repo, check=True)
        c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_repo,
                            capture_output=True, text=True).stdout.strip()
        (tmp_repo / "a.txt").write_text("2\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=tmp_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c2"], cwd=tmp_repo, check=True)
        c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_repo,
                            capture_output=True, text=True).stdout.strip()

        state = tmp_repo / ".deploy-state"

        def base(fallback: str) -> str:
            out = subprocess.run(
                ["bash", "-c",
                 f'. "{PLAN}"; resolve_diff_base {shlex.quote(fallback)} {shlex.quote(str(state))}'],
                cwd=tmp_repo, capture_output=True, text=True, timeout=30,
            )
            if out.returncode != 0:
                raise RuntimeError(f"resolve_diff_base 失败：{out.stderr[:200]}")
            return out.stdout.strip()

        check("状态文件不存在 → 用 fallback", base(c2), c2)
        state.write_text(c1 + "\n", encoding="utf-8")
        check("有状态文件 → 用上次部署的 c1（不是 pull 前的 c2）", base(c2), c1)
        check("关键：基准 ≠ 当前 HEAD，所以能算出变更", base(c2) != c2, True)

        state.write_text("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef\n", encoding="utf-8")
        check("状态文件是无效 commit（force push 后）→ 退回 fallback", base(c2), c2)

        state.write_text("\n  \n", encoding="utf-8")
        check("状态文件是空白 → 退回 fallback", base(c2), c2)

        # 真实场景复现：c1 → c2 之间改过 transcribe.py 吗？用真实 diff 验一次
        (tmp_repo / "transcribe.py").write_text("# x\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=tmp_repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "c3"], cwd=tmp_repo, check=True)
        c3 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_repo,
                            capture_output=True, text=True).stdout.strip()
        state.write_text(c1 + "\n", encoding="utf-8")
        got_base = base(c3)
        diff = subprocess.run(["git", "diff", "--name-only", got_base, c3],
                              cwd=tmp_repo, capture_output=True, text=True).stdout.strip()
        check("基准取 c1 时能看见 transcribe.py 的变更", "transcribe.py" in diff, True)
    finally:
        import shutil as _sh

        _sh.rmtree(tmp_repo, ignore_errors=True)

    print("\n" + "-" * 46)
    print(f"通过 {TOTAL} 项，失败 {FAILED} 项")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
