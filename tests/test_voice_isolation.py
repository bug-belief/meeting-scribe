#!/usr/bin/env python3
"""音色归属隔离的自检。

关键做法：**从源文件里把函数原文抽出来执行**，不复制一份副本。
抄副本的测试永远是绿的——改了源码它也不知道。这里抽完还会做一次反向
对照：故意把判定改成「一律放行」，断言测试必须失败，证明它真的在测东西。
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER = ROOT / "server.py"
TTS = ROOT / "tts_server.py"

PASS = 0
FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}：期望 {want!r}，实际 {got!r}")


def extract_func(src: str, name: str) -> str:
    """按缩进配对抽出 `def <name>(...)` 整个函数（含装饰器以外的函数体）。"""
    lines = src.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.lstrip().startswith(f"def {name}("):
            start = i
            break
    if start is None:
        raise SystemExit(f"源文件里找不到函数 {name}")
    base = len(lines[start]) - len(lines[start].lstrip())
    out = [lines[start]]
    for ln in lines[start + 1:]:
        if not ln.strip():
            out.append(ln)
            continue
        if len(ln) - len(ln.lstrip()) <= base:
            break
        out.append(ln)
    return textwrap.dedent("\n".join(out))


def load(names: list[str], src_path: Path) -> dict:
    """把源文件里的若干函数原文抽出来 exec 进同一个命名空间。"""
    src = src_path.read_text(encoding="utf-8")
    space: dict = {"os": os, "__builtins__": __builtins__}
    for fname in names:
        code = extract_func(src, fname)
        exec(compile(code, f"<{src_path.name}:{fname}>", "exec"), space)
    return space


# ---------------------------------------------------------------- 可见性
def test_visible() -> None:
    print("\n[_voice_visible] 下拉里能看到哪些音色")
    f = load(["_voice_visible"], SERVER)["_voice_visible"]

    system = {"name": "sys:xiaoxiao", "kind": "system"}
    clone = {"name": "alice", "kind": "clone"}
    table = {"alice": "me", "laowang": "mom"}

    check("系统音色对谁都可见", f(system, "mom", table), True)
    check("本人的克隆音色可见", f(clone, "me", table), True)
    check("别人的克隆音色不可见", f(clone, "mom", table), False)
    check("归属表为空（服务没起来）不拦", f(clone, "mom", {}), True)
    check("老音色 owner 为空 → 放行", f({"name": "old", "kind": "clone"}, "mom",
                                      {"old": ""}), True)
    check("表里没这个名字 → 放行（交给 /tts 报错）",
          f({"name": "ghost", "kind": "clone"}, "mom", table), True)
    check("kind 缺省按 clone 处理", f({"name": "alice"}, "mom", table), False)


# ---------------------------------------------------------------- 合成闸门
def test_allowed() -> None:
    print("\n[_voice_allowed] 合成时允许用哪些音色")
    f = load(["_voice_allowed"], SERVER)["_voice_allowed"]
    table = {"alice": "me"}

    check("空 voice 不拦（由服务自己选默认）", f("", "mom", table), True)
    check("系统音色人人可用", f("sys:yunxi", "mom", table), True)
    check("本人音色允许", f("alice", "me", table), True)
    check("他人音色拒绝", f("alice", "mom", table), False)
    check("归属表空 → 不拦（避免误伤）", f("alice", "mom", {}), True)
    check("音色不存在 → 不拦（让 /tts 报「没有这个音色」）",
          f("nope", "mom", table), True)
    check("owner 为空的老音色 → 放行", f("old", "mom", {"old": ""}), True)


# ---------------------------------------------------------------- 路径脱敏
def test_public_voice() -> None:
    print("\n[public_voice] 对外字段里不许出现绝对路径")
    src = TTS.read_text(encoding="utf-8")
    body = extract_func(src, "public_voice")
    body = textwrap.dedent(body).replace("@staticmethod", "").lstrip()
    space: dict = {"os": os, "__builtins__": __builtins__}
    exec(compile(body, "<tts_server:public_voice>", "exec"), space)
    f = space["public_voice"]

    info = {
        "name": "alice",
        "path": "/home/alice/tts/voices/alice/默认/prompt.wav",
        "source": "/home/alice/data/tenants/me/processed/2026-01-01_会议_1000/会议_1000.wav",
        "owner": "me",
    }
    out = f(info)
    check("path 字段被剔除", "path" in out, False)
    check("source 只剩文件名",
          out["source"], "会议_1000.wav")
    check("序列化后不含 /home/", "/home/" in str(out), False)
    check("owner 保留（判定要用）", out.get("owner"), "me")
    check("name 保留", out.get("name"), "alice")

    empty = f({"name": "x", "path": "/a/b.wav", "source": ""})
    check("source 为空不炸", empty.get("source"), "")


# ---------------------------------------------------------------- 反向对照
def test_negative_control() -> None:
    print("\n[反向对照] 把闸门改成「一律放行」，测试必须失败")
    src = SERVER.read_text(encoding="utf-8")
    code = extract_func(src, "_voice_allowed")
    broken = code.replace("return (not mine) or mine == owner", "return True")
    if broken == code:
        print("  ✗ 没找到要破坏的那行，反向对照无效")
        globals()["FAIL"] += 1
        return
    space: dict = {"__builtins__": __builtins__}
    exec(compile(broken, "<broken>", "exec"), space)
    f = space["_voice_allowed"]
    got = f("alice", "mom", {"alice": "me"})
    check("破坏后「他人音色」应被误放行（=True，与正确结果 False 相反）", got, True)
    print("  ↑ 这一项通过说明：上面的「他人音色拒绝」确实在测逻辑，不是恒真")


def main() -> int:
    print("=" * 56)
    print("音色归属隔离自检")
    print("=" * 56)
    test_visible()
    test_allowed()
    test_public_voice()
    test_negative_control()
    print("\n" + "=" * 56)
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    print("=" * 56)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
