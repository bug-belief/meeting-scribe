"""live 工作区留存：扫得到、判得准、不误删正在录的那一场。

为什么单独一个套件
------------------
`deploy/retention.py` 原来只管归档区的音频。2026-10-20 发现 live 会话工作区
（`tenants/<owner>/work/live/<会话id>/`）从来没人清 —— 堆了几十个目录 / 近百 MB，
而归档只有二十场左右，因为 `server.py` 只定义了路径、全代码没有 rmtree。

给 live 加清理的风险不在「删不掉」，而在**删错**：正在录的那一场，
文件还在追加，一旦被判成「很久没动」就会连录音一起搬走。所以这里的
重点是三道护栏，每条都要有反向对照：

  ① 判据用「目录内文件 mtime 最大值」，不是目录自身 mtime
     （Linux 目录 mtime 只在增删条目时更新，持续写 raw.pcm 不会变）；
  ② --live-min-hours 硬下限（默认 24），参数怎么设都不能突破；
  ③ 跑前查 /api/health 的 sessions，明确有会话在跑就整轮跳过。

测试方式遵照本仓铁律：**直接加载源文件里的实现，不抄副本**。
抄一份到测试里，改了源码测试仍然绿 —— 那种绿是假的。
反向对照的做法：这里内联一份「错的实现」（用目录 mtime），
断言它在同一个场景下得到的结论与真实现**不同**，证明这套用例确实能分辨对错。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "deploy" / "retention.py"

PASS = FAIL = 0


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


def load_module():
    """从源文件加载 retention 模块（不是抄一份，改源码这里立刻跟着变）。"""
    spec = importlib.util.spec_from_file_location("retention_under_test", SRC)
    if spec is None or spec.loader is None:
        raise AssertionError(f"加载不了 {SRC}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def touch(path: Path, when: datetime, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    ts = when.timestamp()
    os.utime(path, (ts, ts))
    return path


def age_dir(path: Path, when: datetime) -> None:
    """只改目录自身 mtime，不动里面的文件 —— 用来造「目录老、文件新」的场景。"""
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def build_tree(root: Path) -> dict[str, Path]:
    """造一棵假 data 树。返回各关键路径，供断言引用。"""
    now = datetime.now()
    data = root / "data"
    p: dict[str, Path] = {"data": data}

    # 归档：一个老到该清音频的、一个新的
    p["old_arch"] = data / "tenants" / "me" / "processed" / "2020-01-01_archive-a"
    touch(p["old_arch"] / "archive-a.wav", now - timedelta(days=400))
    touch(p["old_arch"] / "transcript.md", now - timedelta(days=400))
    p["new_arch"] = data / "tenants" / "me" / "processed" / "2099-01-01_archive-b"
    touch(p["new_arch"] / "archive-b.wav", now - timedelta(days=1))

    live = data / "tenants" / "me" / "work" / "live"
    # 8 天没动的（该清）
    p["live_old"] = live / "aaaa11112222"
    touch(p["live_old"] / "raw.pcm", now - timedelta(days=8), b"p" * 100)
    touch(p["live_old"] / "会议_0900.wav", now - timedelta(days=8), b"w" * 200)
    # 1 小时前刚动过（必须留，正在用的那场）
    p["live_fresh"] = live / "bbbb33334444"
    touch(p["live_fresh"] / "raw.pcm", now - timedelta(hours=1), b"p" * 50)
    # 目录 mtime 很老、但文件是新的 —— 专门验「别拿目录 mtime 当判据」
    p["live_tricky"] = live / "cccc55556666"
    touch(p["live_tricky"] / "raw.pcm", now - timedelta(hours=2), b"p" * 30)
    age_dir(p["live_tricky"], now - timedelta(days=30))
    # live/ 下放一个散文件，不该被当成会话目录
    touch(live / "stray.txt", now - timedelta(days=30))
    # 空目录（没文件）
    p["live_empty"] = live / "dddd77778888"
    p["live_empty"].mkdir(parents=True, exist_ok=True)

    # 早期单租户布局
    p["legacy_live"] = data / "work" / "live" / "eeee99990000"
    touch(p["legacy_live"] / "raw.pcm", now - timedelta(days=9), b"p" * 10)

    return p


def main() -> int:  # noqa: C901  测试用例本就长
    mod = load_module()
    tmp = Path(tempfile.mkdtemp(prefix="retention_test_"))
    try:
        p = build_tree(tmp)
        now = datetime.now()

        # ---------- 1. iter_live_dirs 扫得到什么 ----------
        print("\n[iter_live_dirs]")
        found = list(mod.iter_live_dirs(p["data"]))
        names = sorted(d.name for _, d in found)
        check("扫到 5 个会话目录（4 tenant + 1 legacy）", len(found), 5)
        check("目录名齐全", names,
              sorted(["aaaa11112222", "bbbb33334444", "cccc55556666", "dddd77778888",
                      "eeee99990000"]))
        check_true("散文件 stray.txt 没被当目录",
                   "stray.txt" not in names, f"实际 {names}")
        owners = sorted((o or "_legacy") for o, _ in found)
        check_true("legacy 那条 owner 是 None", None in [o for o, _ in found],
                   f"owners={owners}")
        check("不存在 live 的树 → 空", list(mod.iter_live_dirs(tmp / "nope")), [])

        # ---------- 2. last_activity 判据 ----------
        print("\n[last_activity]")
        la_old = mod.last_activity(p["live_old"])
        check_true("8 天前的目录判成 8 天前",
                   abs((now - la_old).total_seconds() - 8 * 86400) < 3600,
                   f"得到 {la_old}")
        la_tricky = mod.last_activity(p["live_tricky"])
        check_true("目录 mtime 30 天前、文件 2 小时前 → 判 2 小时前",
                   abs((now - la_tricky).total_seconds() - 2 * 3600) < 600,
                   f"得到 {la_tricky}（若这里是 30 天前，说明用了目录 mtime，会误删）")
        la_empty = mod.last_activity(p["live_empty"])
        check_true("空目录退回用目录自身 mtime", abs((now - la_empty).total_seconds()) < 60,
                   f"得到 {la_empty}")

        # 反向对照：内联一份「错实现」（用目录 mtime），在同场景必须给出不同答案
        def wrong_last_activity(folder: Path) -> datetime:
            return datetime.fromtimestamp(folder.stat().st_mtime)

        wrong = wrong_last_activity(p["live_tricky"])
        check_true("反向对照：按目录 mtime 的实现会把它误判成 30 天前",
                   abs((now - wrong).total_seconds() - 30 * 86400) < 86400,
                   f"得到 {wrong}")
        check_true("反向对照：两种实现在 tricky 目录上结论不同",
                   abs((now - wrong).total_seconds() - (now - la_tricky).total_seconds()) > 86400,
                   "若两者相同，说明 good 实现退化了")

        # ---------- 3. active_sessions 连不上 ----------
        print("\n[active_sessions]")
        check("连不上 → None（不是 0）", mod.active_sessions("http://127.0.0.1:1/api/health", 0.5), None)
        check("地址非法 → None", mod.active_sessions("not-a-url", 0.5), None)

        # ---------- 4. dry-run：只报告不动手 ----------
        print("\n[dry-run]")
        rc = mod.main(["--data", str(p["data"]), "--health-url", "http://127.0.0.1:1/api/health"])
        check("退出码 0", rc, 0)
        check_true("dry-run 后 live_old 还在原位", p["live_old"].is_dir())
        check_true("dry-run 后归档音频还在", (p["old_arch"] / "archive-a.wav").is_file())
        check_true("dry-run 后没建 trash 目录", not (p["data"] / "trash").exists(),
                   f"{p['data'] / 'trash'}")

        # ---------- 5. apply：真的搬，且搬对地方 ----------
        print("\n[apply]")
        rc = mod.main(["--data", str(p["data"]), "--apply",
                       "--health-url", "http://127.0.0.1:1/api/health"])
        check("退出码 0", rc, 0)
        check_true("8 天没动的 live 目录已搬走", not p["live_old"].exists())
        check_true("1 小时前动过的还在（硬下限 24h）", p["live_fresh"].is_dir(),
                   "被误删了说明 min-hours 没生效")
        check_true("目录 mtime 老但文件新的还在", p["live_tricky"].is_dir(),
                   "被误删了说明用了目录 mtime")
        check_true("空目录（刚建）还在", p["live_empty"].is_dir())
        check_true("legacy live 的 9 天目录已搬走", not p["legacy_live"].exists())
        check_true("散文件 stray.txt 没被动", (p["data"] / "tenants" / "me" / "work" /
                                              "live" / "stray.txt").is_file())

        day = datetime.now().strftime("%Y%m%d")
        moved = p["data"] / "trash" / "me" / day / "live-aaaa11112222"
        check_true("搬到了 trash/me/<今天>/live-<id>", moved.is_dir(), f"查 {moved}")
        check_true("搬过去的内容完整（raw.pcm 在）", (moved / "raw.pcm").is_file())
        check_true("搬过去的内容完整（wav 在）", (moved / "会议_0900.wav").is_file())
        check("文件字节一致", (moved / "raw.pcm").stat().st_size, 100)
        check_true("legacy 搬到 _legacy 下",
                   (p["data"] / "trash" / "_legacy" / day / "live-eeee99990000").is_dir())

        check_true("老归档音频已搬进回收站",
                   (p["data"] / "trash" / "me" / day / "2020-01-01_archive-a" / "archive-a.wav").is_file())
        check_true("归档的文本不动", (p["old_arch"] / "transcript.md").is_file(),
                   "文本必须永久留")
        check_true("新归档音频不动", (p["new_arch"] / "archive-b.wav").is_file())

        # ---------- 6. 参数开关 ----------
        print("\n[参数开关]")
        tmp2 = Path(tempfile.mkdtemp(prefix="retention_sw_"))
        p2 = build_tree(tmp2)
        mod.main(["--data", str(p2["data"]), "--apply", "--live-days", "0",
                  "--health-url", "http://127.0.0.1:1/api/health"])
        check_true("--live-days 0 → 8 天的 live 目录也不动", p2["live_old"].is_dir())
        check_true("--live-days 0 仍照常清归档音频",
                   not (p2["old_arch"] / "archive-a.wav").exists())

        tmp3 = Path(tempfile.mkdtemp(prefix="retention_mh_"))
        p3 = build_tree(tmp3)
        # live-days 压到 0.01 天（14.4 分钟）→ 1 小时前的已「超期」，
        # 但硬下限 24h 仍在，必须保住。这才是硬下限真正有用的场景。
        mod.main(["--data", str(p3["data"]), "--apply", "--live-days", "0.01",
                  "--health-url", "http://127.0.0.1:1/api/health"])
        check_true("live-days 压到 14 分钟，硬下限 24h 仍保住 1 小时前的目录",
                   p3["live_fresh"].is_dir(),
                   "被删了说明 min-hours 没兜住")

        tmp3b = Path(tempfile.mkdtemp(prefix="retention_mh0_"))
        p3b = build_tree(tmp3b)
        # 把硬下限也放开 → 同一个 1 小时前的目录就该被清（对照：证明上面那条
        # 通过是因为硬下限在起作用，不是因为它恰好没超期）
        mod.main(["--data", str(p3b["data"]), "--apply", "--live-days", "0.01",
                  "--live-min-hours", "0",
                  "--health-url", "http://127.0.0.1:1/api/health"])
        check_true("硬下限也设 0 → 1 小时前的目录被清（反证硬下限确实在起作用）",
                   not p3b["live_fresh"].exists())

        # ---------- 7. 有活动会话时整轮跳过 ----------
        print("\n[活动会话保护]")
        tmp4 = Path(tempfile.mkdtemp(prefix="retention_sess_"))
        p4 = build_tree(tmp4)
        real = mod.active_sessions
        mod.active_sessions = lambda *a, **k: 2          # 假装有 2 个会话在跑
        try:
            mod.main(["--data", str(p4["data"]), "--apply",
                      "--health-url", "http://127.0.0.1:1/api/health"])
        finally:
            mod.active_sessions = real
        check_true("有会话在跑 → 8 天的 live 目录一个都不动", p4["live_old"].is_dir(),
                   "误删了说明 sessions 护栏没生效")
        check_true("有会话在跑不影响归档音频清理",
                   not (p4["old_arch"] / "archive-a.wav").exists())

        # ---------- 8. 回收站里的 live- 目录能被 purge 认出来 ----------
        print("\n[回收站兜底]")
        day_dir = p["data"] / "trash" / "me" / day
        check_true("live- 目录落在 <YYYYMMDD> 这一层，purge 能扫到", day_dir.is_dir())
        # purge_trash 是按**目录名**里的日期判过期（不是 mtime），所以要把批次改名
        old_day = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
        day_dir.rename(day_dir.with_name(old_day))
        batches, _ = mod.purge_trash(p["data"] / "trash", 7, dry=True)
        check_true("30 天前那批会被 purge 判到期（live- 前缀不影响判断）", batches >= 1,
                   f"batches={batches}")

        print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
        return 0 if FAIL == 0 else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for extra in ("retention_sw_", "retention_mh_", "retention_mh0_", "retention_sess_"):
            for d in Path(tempfile.gettempdir()).glob(extra + "*"):
                shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
