"""版本号自检：真源唯一、四处一致、缺文件不炸。

为什么值得单独一个套件
----------------------
版本号漂移是「不报错的那种错」：代码改了、APK 还是老版本号、服务端 health
还回上一版，谁都不会发现，直到用户说「我装的到底是哪个版本」。

所以这里的每一条都是「一致性断言」而不是「功能断言」。

测试方式遵照本仓铁律：**从源文件抽函数原文 exec，不抄副本**。
抄一份 read_version 到测试里，改了 config.py 的定位逻辑测试仍然绿——那种绿是假的。
"""

from __future__ import annotations

import ast
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  ✗    {name}\n      得到 {got!r}\n      期望 {want!r}")


def check_true(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  ✗    {name}  {detail}")


def extract_func(src_file: Path, func_name: str) -> str:
    """从源文件里抠出一个函数的原始源码（含装饰器之外的全部内容）。"""
    src = src_file.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"{src_file.name} 里找不到函数 {func_name}")


def extract_constant(src_file: Path, name: str):
    """抠出模块级字面量常量（read_version 依赖 VERSION_FALLBACK）。

    单独抽而不是在测试里硬写 "0.0.0-unknown"：硬写的话，
    改了源码的占位符，测试还是按老的断言通过。
    """
    src = src_file.read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{src_file.name} 里找不到常量 {name}")


def version_mismatches(version: str, manifest: str, build_sh: str) -> list[str]:
    """检查其他位置的版本号是否与真源一致。返回问题列表，空 = 一致。

    单独抽成函数是为了能反向对照：喂给它一份「故意不一致」的输入，
    它必须报出问题——否则这个测试本身就是摆设。
    """
    problems: list[str] = []

    m = re.search(r'android:versionName="([^"]*)"', manifest)
    if not m:
        problems.append("AndroidManifest 里没有 versionName")
    elif m.group(1) != version:
        problems.append(f"AndroidManifest versionName={m.group(1)} ≠ VERSION={version}")

    if "APP_DIR/VERSION" not in build_sh and "$APP_DIR/VERSION" not in build_sh:
        problems.append("build_apk.sh 没有从 VERSION 读版本号")
    if "tr -d" not in build_sh:
        problems.append("build_apk.sh 没清掉 VERSION 里的换行（会把 \\n 带进 versionName）")

    return problems


def main() -> int:
    # 抽源码原文，而不是 import——import 会把整个 config 的副作用带进来。
    # exec 环境必须补 __file__：read_version 用 Path(__file__).parent 定位仓库根，
    # 少了它会在调用时 NameError（第一次跑就踩到了）。
    src = extract_func(ROOT / "config.py", "read_version")
    fallback = extract_constant(ROOT / "config.py", "VERSION_FALLBACK")
    ns: dict = {
        "Path": Path,
        "__file__": str(ROOT / "config.py"),
        "VERSION_FALLBACK": fallback,
    }
    exec(compile(src, "config.py:read_version", "exec"), ns)  # noqa: S102
    read_version = ns["read_version"]

    build_src = extract_func(ROOT / "config.py", "read_build")
    ns_build: dict = {
        "Path": Path,
        "__file__": str(ROOT / "config.py"),
        "read_version": read_version,
        "subprocess": __import__("subprocess"),
    }
    exec(compile(build_src, "config.py:read_build", "exec"), ns_build)  # noqa: S102
    read_build = ns_build["read_build"]

    print("\n1) VERSION 文件本身")
    vpath = ROOT / "VERSION"
    check_true("VERSION 文件存在", vpath.is_file())
    raw = vpath.read_text(encoding="utf-8") if vpath.is_file() else ""
    version = raw.strip()
    check_true(f"内容是合法 semver（{version!r}）", bool(SEMVER.match(version)), version)
    check_true("文件里没有多余空行（只有一行 + 末尾换行）",
               raw.count("\n") == 1 and raw.endswith("\n"), repr(raw))

    print("\n2) read_version 从 VERSION 读，且不依赖 cwd")
    check("read_version() == VERSION 内容", read_version(), version)
    check("显式传 base_dir 也一致", read_version(ROOT), version)

    print("\n3) 反向对照：读不到文件必须返回占位符而不是抛异常")
    # 探活接口不能因为少一个文件就 500，这条走的是 fallback 分支
    with tempfile.TemporaryDirectory() as td:
        check("空目录 → 占位符", read_version(Path(td)), "0.0.0-unknown")
        empty = Path(td) / "VERSION"
        empty.write_text("   \n", encoding="utf-8")
        check("文件只有空白 → 占位符", read_version(Path(td)), "0.0.0-unknown")

    print("\n4) read_build 给鉴权接口用的三件套")
    info = read_build()
    check_true("有 version/commit/dirty 三个键",
               set(info) >= {"version", "commit", "dirty"}, str(info))
    check("version 与 VERSION 一致", info["version"], version)
    check_true("dirty 是布尔", isinstance(info["dirty"], bool), type(info["dirty"]).__name__)

    print("\n5) 一致性：AndroidManifest / build_apk.sh 是否跟着真源走")
    manifest = (ROOT / "android" / "AndroidManifest.xml").read_text(encoding="utf-8")
    build_sh = (ROOT / "deploy" / "build_apk.sh").read_text(encoding="utf-8")
    check("当前四处一致（问题列表为空）", version_mismatches(version, manifest, build_sh), [])

    print("\n6) 反向对照：故意造不一致，检查函数必须报出来")
    bad_manifest = manifest.replace(f'android:versionName="{version}"',
                                    'android:versionName="9.9.9"')
    check_true("manifest 版本落后会被抓到",
               any("9.9.9" in p for p in version_mismatches(version, bad_manifest, build_sh)),
               str(version_mismatches(version, bad_manifest, build_sh)))
    no_read = build_sh.replace("$APP_DIR/VERSION", "/dev/null")
    check_true("build_apk.sh 不读 VERSION 会被抓到",
               any("没有从 VERSION" in p for p in version_mismatches(version, manifest, no_read)),
               str(version_mismatches(version, manifest, no_read)))
    check_true("完全没有 versionName 会被抓到",
               any("没有 versionName" in p
                   for p in version_mismatches(version, "<manifest/>", build_sh)))

    print("\n7) 服务端确实把版本号暴露出来了")
    # O-11 拆 api/ 之后，路由不再写在 server.py 里，**源码切片式断言失效**
    # （切片找不到 @app.get("/api/health") 就抛 ValueError）。
    # 这本来就是这类测试的老毛病：断言绑死实现位置，搬一次代码就假红/假绿。
    # 改成**按行为断言**：真起一个 app，打真实请求看返回字段。
    all_src = "\n".join(
        (ROOT / f).read_text(encoding="utf-8")
        for f in ("server.py", "api/health.py")
        if (ROOT / f).is_file()
    )
    check_true("服务端用 read_version 初始化 _APP_VERSION",
               bool(re.search(r"_APP_VERSION\s*=\s*read_version\(\)", all_src)))

    try:
        # ⚠️ 这里不要 `import tempfile` / `from pathlib import Path`——
        # 文件顶部已导入，函数内再导入会让它们在整个函数里变成局部名，
        # 前面的 `with tempfile.TemporaryDirectory()` 就报 UnboundLocalError。
        # （第一版就这么错了，UnboundLocalError 很难一眼看出是这个原因）
        from dataclasses import replace

        from fastapi.testclient import TestClient

        sys.path.insert(0, str(ROOT))
        import server
        from config import SETTINGS

        cfg = replace(SETTINGS, base_dir=Path(tempfile.mkdtemp()), web_token="__T__")
        client = TestClient(server.create_app(cfg))
        with client:
            h = client.get("/api/health").json()
            check("/api/health 的 version == VERSION 文件", h.get("version"), version)
            check_true("/api/health 不返回 build（git 提交号只给鉴权接口）",
                       "build" not in h)
            m = client.get("/api/me?k=__T__").json()
            check_true("/api/me 返回 build", isinstance(m.get("build"), dict), str(m)[:200])
            check("/api/me 的 build.version == VERSION 文件",
                  (m.get("build") or {}).get("version"), version)
            # B-5：/api/me 不得吐服务器路径
            check_true("/api/me 不返回 root 路径（B-5）",
                       "root" not in m, str(m)[:200])
    except ImportError as exc:  # pragma: no cover
        print(f"  SKIP 缺 fastapi/httpx，跳过行为断言：{exc}")

    print("\n8) 配置打印不能把口令印出来")
    cfg_src = (ROOT / "config.py").read_text(encoding="utf-8")
    main_block = cfg_src[cfg_src.index('if __name__ == "__main__":'):]
    check_true("web_token 在打码名单里", '"web_token"' in main_block, main_block[:300])
    check_true("tenant_token_map 在打码名单里", '"tenant_token_map"' in main_block)

    print("\n" + "=" * 40)
    total = PASS + FAIL
    print(f"通过 {PASS} 项，失败 {FAIL} 项（共 {total} 项）")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
