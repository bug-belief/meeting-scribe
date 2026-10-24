"""O-14 硬编码收敛：端口与监听地址必须只有一个真源（Settings），不许再写死。

背景：收敛前 8848 / 9880 散落在 4 个文件、6 个位置——
`server.py` argparse 默认值、`tts_server.py` argparse 默认值、
`voice_register.py` 的 `--asr` / `--tts` 默认值、`.env.example` 的注释里。
改端口要改多处，漏一处就是「配置写了 9999，实际还监听 8848」。

本测试不导入 fastapi/torch（本机没装），
用**源码静态分析 + 隔离子进程**两条腿验，缺一不可：
  ① 静态：AST 扫描生产代码，argparse 的 default 里不许出现端口字面量
  ② 动态：真跑 config.load_settings()，改环境变量看值是否跟着变
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

FAILED = 0


def check(name: str, got, want=True) -> bool:
    global FAILED
    ok = got == want
    if not ok:
        FAILED += 1
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f"  ← 实际 {got!r}，期望 {want!r}"))
    return ok


def check_true(name: str, cond: bool) -> bool:
    return check(name, bool(cond), True)


# ---------------------------------------------------------------------------
print("\n1) config.Settings 有三个新字段，且默认值与收敛前一致")
sys.path.insert(0, str(ROOT))
from config import SETTINGS  # noqa: E402

check("web_host 默认 0.0.0.0", SETTINGS.web_host, "0.0.0.0")
check("web_port 默认 8848", SETTINGS.web_port, 8848)
check("tts_port 默认 9880", SETTINGS.tts_port, 9880)


# ---------------------------------------------------------------------------
print("\n2) 环境变量能覆盖（反向对照：不设变量时必须是默认值）")

code = (
    "import sys; sys.path.insert(0, r'%s');"
    "from config import load_settings;"
    "s = load_settings(); print(s.web_port, s.tts_port, s.web_host)"
) % ROOT


def run_env(**env):
    e = dict(os.environ)
    e.pop("WEB_PORT", None)
    e.pop("TTS_PORT", None)
    e.pop("WEB_HOST", None)
    e.update(env)
    out = subprocess.run([PY, "-c", code], capture_output=True, text=True, env=e, timeout=120)
    return out.stdout.strip()


check("不设变量 → 8848 9880 0.0.0.0", run_env(), "8848 9880 0.0.0.0")
check("WEB_PORT=9999 生效", run_env(WEB_PORT="9999").split()[0], "9999")
check("TTS_PORT=9998 生效", run_env(TTS_PORT="9998").split()[1], "9998")
check("WEB_HOST=127.0.0.1 生效", run_env(WEB_HOST="127.0.0.1").split()[2], "127.0.0.1")


# ---------------------------------------------------------------------------
print("\n3) 生产代码里 argparse 的 default 不许是端口/地址字面量（AST 扫描）")

# 这些文件是「生产代码」，改了就得跟着改配置；tests/ 与 docs/ 不在此列
PROD = ["server.py", "tts_server.py", "voice_register.py", "watcher.py", "config.py"]
# 允许出现的字面量：不是端口的东西
PORT_ADD_RE = re.compile(r"127\.0\.0\.1:\d+|0\.0\.0\.0|^\d{4,5}$")

offenders = []
for fname in PROD:
    path = ROOT / fname
    if not path.is_file():
        continue
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        # 找 add_argument(..., default=<字面量>)
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        argname = ""
        for a in node.args:
            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                argname = a.value
                break
        for kw in node.keywords:
            if kw.arg != "default":
                continue
            v = kw.value
            # 只关心 --port / --asr / --tts 这类「会漂移」的默认值
            #  --host 不在此列：tts_server 固定只听本机 127.0.0.1 是安全约束，
            #  把它挪进配置反而给了「一不小心把合成服务暴露到公网」的机会
            if argname.lstrip("-") not in {"port", "asr", "tts"}:
                continue
            if isinstance(v, ast.Constant):
                offenders.append((fname, argname, v.value))

check("server.py / tts_server.py / voice_register.py 的 port/asr/tts 默认值都是变量引用",
      offenders, [])


# ---------------------------------------------------------------------------
print("\n4) 容器型文件（.service / .sh）里的端口必须与 Settings 一致，或显式说明")

# .service 里显式写 --port 是可以的（运维要能一眼看到），但值必须等于默认值，
# 否则「改配置不生效」会在部署时咬人。
svc = ROOT / "deploy" / "meeting-web.service"
if svc.is_file():
    txt = svc.read_text(encoding="utf-8")
    m = re.search(r"--port\s+(\d+)", txt)
    check_true("meeting-web.service 里有 --port", m is not None)
    if m:
        check("meeting-web.service 的端口 == SETTINGS.web_port",
              int(m.group(1)) == SETTINGS.web_port)


# ---------------------------------------------------------------------------
print("\n5) voice_register.py 在缺 config 的环境下能起来（回退分支，反向对照）")

import shutil
import tempfile

with tempfile.TemporaryDirectory() as td:
    shutil.copy(ROOT / "voice_register.py", Path(td) / "voice_register.py")
    r = subprocess.run(
        [PY, "-c",
         "import sys; sys.path.insert(0, r'%s'); import voice_register as v;"
         "print(v._WEB_PORT, v._TTS_PORT)" % td],
        capture_output=True, text=True, cwd=td, timeout=120,
    )
    check("隔离目录（无 config.py）回退到 8848 9880", r.stdout.strip(), "8848 9880")

# 有 config 时必须读到 Settings 的值，而不是回退值
r = subprocess.run(
    [PY, "-c",
     "import sys; sys.path.insert(0, r'%s'); import voice_register as v;"
     "from config import SETTINGS as S;"
     "print(v._WEB_PORT == S.web_port and v._TTS_PORT == S.tts_port)" % ROOT],
    capture_output=True, text=True, cwd=str(ROOT), timeout=120,
)
check("仓库内 _WEB_PORT/_TTS_PORT 来自 Settings", r.stdout.strip(), "True")


# ---------------------------------------------------------------------------
print("\n" + "=" * 40)
total = 14
print(f"通过 {total - FAILED} 项，失败 {FAILED} 项（共 {total} 项）")
sys.exit(1 if FAILED else 0)
