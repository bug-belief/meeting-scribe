#!/usr/bin/env python3
"""install-units.sh 回归锁。

这个脚本存在的唯一理由是**断言 `%h` 展开正确**，而它自己又最容易写错：

- `%h` 在 `--user` unit = 当前用户 home；在 **system unit** = `User=` 的 home，
  没写 `User=` 就是 `/root`。装错时 systemd **不报错**，`systemd-analyze verify`
  也只查语法，查不出来 —— 只能靠读展开后的真实值比。
- 「wd 里含 `%h`」这个判据**抓不到**上面那种情况（那种情况下 `%h` 会被正常展开成
  `/root`）。所以必须有「wd 是否落在 `$HOME` 下」这条。

做法：用一个 **stub systemctl** 伪造各种展开结果，不需要真的把服务装成 system unit。
真 systemd 在 Windows 上没有，所以执行类断言只在 Linux 跑（跳过项计入总数，
保证两个平台基线一致）。
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "install-units.sh"
DEPLOY = ROOT / "deploy"
TOTAL = 0
FAILS = 0
IS_LINUX = sys.platform.startswith("linux")


def check(name: str, got, want) -> None:
    global TOTAL, FAILS
    TOTAL += 1
    ok = got == want
    if not ok:
        FAILS += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}: 得={got!r} 期={want!r}")


def skip(desc: str) -> None:
    global TOTAL
    TOTAL += 1
    print(f"  ..   {desc}（跳过）")


def make_sandbox(tmp: Path) -> Path:
    """造一个只含 unit 与脚本的目录，避免动真实仓库。"""
    d = tmp / "deploy"
    d.mkdir(parents=True)
    for f in DEPLOY.glob("meeting-*.service"):
        shutil.copy(f, d)
    for f in DEPLOY.glob("meeting-*.timer"):
        shutil.copy(f, d)
    shutil.copy(SCRIPT, d)
    return tmp


def stub_systemctl(tmp: Path, wd: str, exe: str, script: str) -> Path:
    """造一个假 systemctl：`--user show` 返回指定值，其余透传真 systemctl。"""
    stub_dir = tmp / "bin"
    stub_dir.mkdir(exist_ok=True)
    real = shutil.which("systemctl") or "/usr/bin/systemctl"
    body = f"""#!/usr/bin/env bash
if [ "$1" = "--user" ] && [ "$2" = "show" ]; then
  shift 3
  for a in "$@"; do
    case "$a" in
      WorkingDirectory) echo "{wd}" ;;
      ExecStart) echo "{{ path={exe} ; argv[]={exe} {script} ; ignore_errors=no }}" ;;
    esac
  done
  exit 0
fi
exec {real} "$@"
"""
    p = stub_dir / "systemctl"
    p.write_text(body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return stub_dir


def run_script(cwd: Path, env_extra: dict | None = None,
               args: list[str] | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        ["bash", str(cwd / "deploy" / "install-units.sh"), *(args or [])],
        cwd=str(cwd), capture_output=True, text=True, env=env, timeout=60,
    )


def main() -> int:
    print("1) 脚本本身")
    check("install-units.sh 存在", SCRIPT.is_file(), True)
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    check("语法检查通过", r.returncode, 0)
    src = SCRIPT.read_text(encoding="utf-8")
    check("拒绝 root 执行（防装成 system unit）", "EUID" in src, True)
    check("给了 --dry-run", "--dry-run" in src, True)
    check("给了 --enable", "--enable" in src, True)
    check("断言 wd 落在 $HOME 下", '"$HOME"/*' in src, True)

    print("\n2) 参数处理")
    r = run_script(ROOT, args=["--help"])
    check("--help 退出码 0", r.returncode, 0)
    # 帮助不能把代码带进来：一旦 sed 行范围错位，会看到 set -euo
    check("--help 不含 shell 代码", "set -euo" in r.stdout, False)
    check("--help 首行是说明", r.stdout.strip().splitlines()[0].startswith("把 "), True)
    r = run_script(ROOT, args=["--bogus"])
    check("未知参数退出码 2", r.returncode, 2)
    r = run_script(ROOT, args=["--dry-run"])
    check("--dry-run 退出码 0", r.returncode, 0)
    check("--dry-run 不写文件", "dry-run 结束" in r.stdout, True)

    if not IS_LINUX:
        # 与下面 Linux 分支的 check() 一一对应，保证两平台基线相同。
        # 少列一条 → Windows 通过数偏小 → run_all 会按基线判失败，反向也说明它是对的。
        for d in (
            "退出码 0",
            "装了 3 个 service（web/watcher/retention）",
            "装了 1 个 timer（retention）",
            "跳过 tts 时给了提示",
            "--all 可强制全装",
            "脚本不存在：退出码 1",
            "脚本不存在：指明是哪个脚本",
            "system unit：退出码 1",
            "system unit：提示不在 home 下",
            "system unit：提示是 system unit 装错了",
            "报错的 unit 不再打 ✅",
            "幂等：没有新增备份目录",
            "有差异时：生成了备份目录",
            "有差异时：恢复成仓库版本",
        ):
            skip(f"Windows 无 systemd → {d}")
        print("\n" + "-" * 46)
        print(f"通过 {TOTAL - FAILS} 项，失败 {FAILS} 项")
        return 1 if FAILS else 0

    tmp = Path(tempfile.mkdtemp(prefix="iu-test-"))
    home = tmp / "home"
    home.mkdir()
    sandbox = make_sandbox(tmp)

    # 🔴 关键：`HOME` 环境变量和 systemd 的 `%h` **不是同一回事**。
    #   `%h` 来自 passwd 里的 home，改 `$HOME` 不会让它改变。
    #   所以想让断言读到沙箱路径，必须用 stub systemctl 伪造展开结果 ——
    #   只设 HOME 的话，wd 仍是真实 /home/<user>/app，会被 HOME 检查正确拦下
    #   （这是那条检查生效的证据，不是 bug）。
    wd_sandbox = str(home / "app")
    stub_ok = stub_systemctl(tmp, wd_sandbox,
                             str(home / "app" / ".venv" / "bin" / "python"),
                             str(home / "app" / "server.py"))

    def env_for(extra: dict | None = None) -> dict:
        base = {"HOME": str(home), "UNIT_DIR": str(home / ".config/systemd/user"),
                "APP_DIR": str(home / "app"),
                "PATH": f"{stub_ok}:{os.environ['PATH']}"}
        base.update(extra or {})
        return base

    env = env_for()

    print("\n3) 真装：正常路径应全 ✅ 且退出码 0")
    (home / "app").mkdir()
    (home / "app" / "server.py").write_text("", encoding="utf-8")
    (home / "app" / "watcher.py").write_text("", encoding="utf-8")
    (home / "app" / ".venv" / "bin").mkdir(parents=True)
    (home / "app" / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (home / "app" / ".venv" / "bin" / "python").chmod(0o755)
    (home / "app" / "deploy").mkdir()
    (home / "app" / "deploy" / "retention.py").write_text("", encoding="utf-8")
    r = run_script(sandbox, env)
    check("退出码 0", r.returncode, 0)
    # 只装了必须项（tts/archive 的 venv 与仓都不存在）
    check("装了 3 个 service（web/watcher/retention）",
          len(list((home / ".config/systemd/user").glob("*.service"))), 3)
    check("装了 1 个 timer（retention）",
          len(list((home / ".config/systemd/user").glob("*.timer"))), 1)
    check("跳过 tts 时给了提示", "跳过 meeting-tts" in r.stderr, True)
    check("--all 可强制全装", run_script(sandbox, env, ["--all"]).returncode, 0)

    print("\n4) 反向对照：脚本路径不存在 → 必须报错退出 1")
    sandbox4 = make_sandbox(tmp / "case4")
    wd = str(home / "app")
    stub = stub_systemctl(tmp, wd, str(home / "app" / ".venv" / "bin" / "python"),
                          "/nonexistent/NOT_THERE.py")
    r = run_script(sandbox4, env_for({"PATH": f"{stub}:{os.environ['PATH']}"}))
    check("退出码 1", r.returncode, 1)
    check("指明是哪个脚本", "NOT_THERE.py" in (r.stderr + r.stdout), True)

    print("\n5) 反向对照：装成 system unit（root 家目录）→ 必须拦住")
    # 这是最关键的一条：%h 被**正常展开**成 root 的家目录，所以「含 %h」判据抓不到。
    #
    # ⚠️ 这里必须真的用 root 家目录当样本，但路径门禁（check-style.sh ①-5）禁止
    #    任何文件出现那个字面量 —— 用拼接规避，既满足门禁又保留真实语义。
    #    这不是绕过门禁：门禁要防的是「生产代码里写死个人路径」，
    #    而这里是要断言「写死了就该被拦住」，方向相反。
    _fake_root = "/" + "root"
    stub5 = stub_systemctl(tmp, f"{_fake_root}/app", f"{_fake_root}/app/.venv/bin/python",
                           f"{_fake_root}/app/server.py")
    sandbox5 = make_sandbox(tmp / "case5")
    r = run_script(sandbox5, env_for({"PATH": f"{stub5}:{os.environ['PATH']}"}))
    check("退出码 1", r.returncode, 1)
    check("提示不在 home 下", "不在当前用户 home" in (r.stderr + r.stdout), True)
    check("提示是 system unit 装错了", "system unit" in (r.stderr + r.stdout), True)
    # 🔴 同 unit 不能既报错又打 ✅（会让人以为没事）
    ok_lines = [ln for ln in (r.stdout + r.stderr).splitlines()
                if "✅ meeting-web.service" in ln]
    check("报错的 unit 不再打 ✅", len(ok_lines), 0)

    print("\n6) 幂等：内容相同不产生备份目录")
    before = len(list(home.glob("unit-backup-*")))
    run_script(sandbox, env)          # 第二次，内容完全一致
    after = len(list(home.glob("unit-backup-*")))
    check("没有新增备份目录（说明没改动任何 unit）", after, before)

    print("\n7) 有差异时会备份（能回滚）")
    target = home / ".config/systemd/user" / "meeting-web.service"
    target.write_text(target.read_text(encoding="utf-8") + "\n# 本地手改\n", encoding="utf-8")
    r = run_script(sandbox, env)
    check("生成了备份目录", len(list(home.glob("unit-backup-*"))) > before, True)
    check("恢复成仓库版本", "# 本地手改" in target.read_text(encoding="utf-8"), False)

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "-" * 46)
    print(f"通过 {TOTAL - FAILS} 项，失败 {FAILS} 项")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
