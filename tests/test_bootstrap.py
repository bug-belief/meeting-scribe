#!/usr/bin/env python3
"""P2-6 首启自检回归锁。

两个真实风险，都靠「不报错、但结果错」生效，所以必须有护栏：

1. **数据目录落在仓库里** —— `git status` 一片红，`git clean -fdx` 连录音一起删，
   提交时把别人的录音带上公网。现在必须拒启动。
2. **监听 0.0.0.0 但没配口令** —— `resolve_owner()` 的空口令语义是「本地调试直接放行」，
   等于把录音、声纹、纪要全部公开。现在必须自动生成。

判定逻辑都是纯函数；副作用（写 .env、改 settings）单独测，用临时目录，不碰真文件。
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import bootstrap as B              # noqa: E402
import config as C                 # noqa: E402

TOTAL = 0
FAILS = 0


def check(name: str, got, want) -> None:
    global TOTAL, FAILS
    TOTAL += 1
    ok = got == want
    if not ok:
        FAILS += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}: 得={got!r} 期={want!r}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="boot-"))

    print("1) is_inside：用 resolve 之后比，不能只看字符串前缀")
    repo = tmp / "app"
    (repo / "sub").mkdir(parents=True)
    check("子目录算在里面", B.is_inside(repo / "data", repo), True)
    check("自己算在里面（相等）", B.is_inside(repo, repo), True)
    check("父目录不在子目录里", B.is_inside(repo, repo / "data"), False)
    check("兄弟目录不算", B.is_inside(tmp / "data", repo), False)
    # 关键：字面不以 app 开头，但解析后在里面
    check(".. 穿越后仍判在里面", B.is_inside(repo / "sub" / ".." / "data", repo), True)
    check("不存在的路径不误拦", B.is_inside(tmp / "nope" / "x", repo), False)

    print("\n2) needs_token：回环地址不需要，对外监听必须有")
    for host, has, want in [
        ("0.0.0.0", False, True),
        ("0.0.0.0", True, False),
        ("127.0.0.1", False, False),
        ("localhost", False, False),
        ("::1", False, False),
        ("", False, False),
        ("203.0.113.9", False, True),   # RFC 5737 文档保留段：非回环即可，不用真内网段
        (" 0.0.0.0 ", False, True),        # 带空格也要判对
        ("LOCALHOST", False, False),       # 大小写
    ]:
        check(f"needs_token({host!r}, has={has})", B.needs_token(host, has), want)

    print("\n3) 数据目录在仓库内 → 必须拒启动，且报错要能照着修")
    inside = replace(C.SETTINGS,
                     base_dir=repo,
                     processed_dir=repo / "processed",
                     inbox_dir=repo / "audios_inbox",
                     work_dir=repo / "work",
                     logs_dir=repo / "logs",
                     tenant_root=repo / "tenants",
                     speaker_store=repo / "speakers.json")
    err = None
    try:
        B.check_data_outside_repo(inside)
    except B.BootstrapError as exc:
        err = str(exc)
    check("抛了 BootstrapError", err is not None, True)
    check("说明了原因（git 风险）", "git" in (err or ""), True)
    check("给了具体字段名", "PROCESSED_DIR" in (err or ""), True)
    check("给了修法", "setup-env.sh" in (err or ""), True)

    print("\n4) 数据目录在仓库外 → 放行（不能误拦）")
    outside = replace(C.SETTINGS,
                      base_dir=repo,
                      processed_dir=tmp / "data" / "processed",
                      inbox_dir=tmp / "data" / "audios_inbox",
                      work_dir=tmp / "data" / "work",
                      logs_dir=tmp / "data" / "logs",
                      tenant_root=tmp / "data" / "tenants",
                      speaker_store=tmp / "data" / "speakers.json")
    ok = True
    try:
        B.check_data_outside_repo(outside)
    except B.BootstrapError:
        ok = False
    check("正常布局不报错", ok, True)

    print("\n5) 没口令 + 对外监听 → 自动生成并写进 .env")
    envf = tmp / "test.env"
    envf.write_text("# 用户自己的配置\nASR_BACKEND=cloud\n", encoding="utf-8")
    s5 = replace(outside, web_token="", tenant_token_map={})
    tok = B.check_web_token(s5, "0.0.0.0", envf)
    check("生成了口令", bool(tok), True)
    check("长度够（urlsafe(24) ≈ 32 字符）", len(tok) >= 30, True)
    check("已写到 settings（本进程立刻生效）", s5.web_token, tok)
    text = envf.read_text(encoding="utf-8")
    check(".env 里有该口令", f"WEB_TOKEN={tok}" in text, True)
    check("没有破坏原有配置", "ASR_BACKEND=cloud" in text, True)
    # ⚠️ Windows 的 os.chmod 只影响只读位，属主/组/其他三组权限位读回来恒为 666
    #    （实测：chmod(600) 之后 stat 仍是 0o666）。所以这条只能在 Linux 上判，
    #    否则会得到一个「永远失败」的假红，反而让人不再信这个测试。
    if sys.platform != "win32":
        check("文件权限 600", oct(envf.stat().st_mode & 0o777), oct(0o600))
    else:
        check("Windows 不做权限断言（chmod 语义不同）", True, True)

    print("\n6) 已有口令 → 不覆盖、不生成")
    s6 = replace(outside, web_token="my-own-token", tenant_token_map={})
    tok2 = B.check_web_token(s6, "0.0.0.0", envf)
    check("没生成新口令", tok2, "")
    check("原口令未变", s6.web_token, "my-own-token")
    check(".env 未被改写", envf.read_text(encoding="utf-8").count("WEB_TOKEN="), 1)

    print("\n7) 只监听本机 → 不生成口令，但要说清「谁都能进」")
    s7 = replace(outside, web_token="", tenant_token_map={})
    rep = B.BootstrapReport()
    tok3 = B.check_web_token(s7, "127.0.0.1", tmp / "loopback.env", rep)
    check("没生成", tok3, "")
    check("有权重提示", any("默认租户放行" in n for n in rep.notes), True)

    print("\n8) 多租户已配也算「有口令」")
    s8 = replace(outside, web_token="", tenant_token_map={"abc": "me"})
    check("token_configured 为真", B.token_configured(s8), True)
    check("不生成新口令", B.check_web_token(s8, "0.0.0.0", tmp / "t8.env"), "")

    print("\n9) 幂等：连跑两次不会追加两行")
    envf9 = tmp / "idem.env"
    s9 = replace(outside, web_token="", tenant_token_map={})
    t1 = B.check_web_token(s9, "0.0.0.0", envf9)
    s9b = replace(outside, web_token="", tenant_token_map={})
    t2 = B.check_web_token(s9b, "0.0.0.0", envf9)
    check("第二次仍生成（内存里是空的）", bool(t2), True)
    check("但 .env 只有一行 WEB_TOKEN", envf9.read_text(encoding="utf-8").count("WEB_TOKEN="), 1)
    check("且保留的是第一次的值", f"WEB_TOKEN={t1}" in envf9.read_text(encoding="utf-8"), True)

    print("\n10) 反向对照：把判定改坏，第 3 组必须变红")
    orig = B.is_inside
    B.is_inside = lambda child, parent: False          # 永远「不在仓库里」
    try:
        caught = False
        try:
            B.check_data_outside_repo(inside)
        except B.BootstrapError:
            caught = True
        check("预检失效时确实漏过（证明第 3 组不是恒真）", caught, False)
    finally:
        B.is_inside = orig

    print("\n11) 反向对照：needs_token 恒 False 时第 5 组会漏")
    orig_nt = B.needs_token
    B.needs_token = lambda host, has: False
    try:
        s11 = replace(outside, web_token="", tenant_token_map={})
        check("失效时不再生成口令", B.check_web_token(s11, "0.0.0.0", tmp / "x.env"), "")
    finally:
        B.needs_token = orig_nt

    print("\n12) TTS 探测：启动竞争不能误报，真连不上要报")
    # 开一个真 socket 监听，模拟「tts 正在跑」
    import socket as _sk
    srv = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    live_port = srv.getsockname()[1]
    try:
        s_live = replace(outside, tts_base_url=f"http://127.0.0.1:{live_port}")
        hits = B.report_optional(s_live, probe_retries=2, probe_delay=0.01)
        check("服务在跑 → 不报 TTS 问题",
              any("语音合成服务" in m for m in hits), False)
    finally:
        srv.close()

    # 关掉后端口必然连不上（拿一个刚释放的端口）
    s_dead = replace(outside, tts_base_url=f"http://127.0.0.1:{live_port}")
    dead = [m for m in B.report_optional(s_dead, probe_retries=2, probe_delay=0.05)
            if "语音合成服务" in m]
    check("服务停了 → 必须报出来（重试不能变成沉默）", len(dead), 1)
    check("提示里给了启动命令", "meeting-tts" in (dead[0] if dead else ""), True)
    check("说明是重试过的", "重试" in (dead[0] if dead else ""), True)

    print("\n12b) 缺 python-dotenv：必须报，且排在最前")
    # 为什么这条重要：缺 funasr 只是少个功能；缺 dotenv 会让 **.env 整个不被读**，
    # 所有配置静默退回默认值（含 LLM_API_KEY / WEB_TOKEN / 数据目录）。
    # 2026-10-24 陌生人体检时撞到：配了 .env 却不生效，系统一声不响。
    orig_has = B._has_module
    try:
        B._has_module = lambda n: False if n == "dotenv" else orig_has(n)
        msgs_nodotenv = B.report_optional(outside, probe_retries=1, probe_delay=0.01)
    finally:
        B._has_module = orig_has
    d = [m for m in msgs_nodotenv if "dotenv" in m]
    check("缺 dotenv → 报出来", len(d), 1)
    check("说明了后果（.env 不被读取）", ".env" in (d[0] if d else ""), True)
    check("给了安装命令", "pip install" in (d[0] if d else ""), True)
    check("排在第一条（比其他缺依赖更严重）",
          bool(d) and msgs_nodotenv and msgs_nodotenv[0] == d[0], True)
    # 反向对照：装了 dotenv 时不该出现这条（本例环境确实装了）
    has_now = [m for m in B.report_optional(outside, probe_retries=1, probe_delay=0.01)
               if "dotenv" in m]
    check("dotenv 可用时不误报", has_now, [])

    print("\n13) 端到端：run() 在坏布局下抛错、好布局下返回报告")
    rep_ok = B.run(outside, "127.0.0.1", tmp / "run.env")
    check("好布局 ok=True", rep_ok.ok, True)
    bad = None
    try:
        B.run(inside, "127.0.0.1")
    except B.BootstrapError:
        bad = True
    check("坏布局被拦", bad, True)

    print("\n" + "-" * 46)
    print(f"通过 {TOTAL - FAILS} 项，失败 {FAILS} 项")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
