#!/usr/bin/env python3
"""留存清理，管两个地方：归档区的音频 + live 会话工作区。

两层不要混
----------
① **归档区** `tenants/<owner>/processed/<日期_标题>/`
   这是成品。文本（transcript.md / report.md / transcript.json / meta.json /
   speakers.json）几十 KB，几十场也才 0.1 MB 量级 —— 留着不花钱，删了不可再生，**永久留**。
   音频 2~5 MB/场，是可以重采的，按 --days（默认 60 天）到期清掉。

② **live 工作区** `tenants/<owner>/work/live/<会话id>/`
   这是会话进行中的临时加工区，只有 raw.pcm 和临时 wav，**没有文本**。
   会话结束后没人清就成了纯垃圾 —— 实测堆了几十个目录 / 近百 MB，
   而归档只有二十场左右，是代码里压根没有清理逻辑（server.py 只定义路径、无 rmtree）。
   按 --live-days（默认 7 天）整目录搬走。
   这里可以整目录搬，因为音频正本已经在归档里（约九成归档自带音频），
   回放读的是 processed/（/api/latest），不读 live。

清理不是直接 rm：
  两处都先 mv 进 data/trash/<owner>/<YYYYMMDD>/，
  在回收站再放 --trash-days 天（默认 7）才真删。给「删错了」留反悔窗口。
  live 目录进回收站时带 `live-` 前缀，跟同日归档批次撞名概率归零。

收藏豁免：
  归档目录下有 .favorite 标记（页面上点星标会生成），整个目录跳过，音频不动。
  这是唯一的作用，收藏不影响文本保留（文本本来就不清）。

live 的两道保险（防误删正在录的那一场）：
  ① `--live-min-hours`（默认 24）：无论 --live-days 设多小，24 小时内动过的不碰；
  ② 跑前查 /api/health 的 sessions，明确有会话在跑就整轮跳过 live。
  另外判「最后活动」用的是**目录内文件 mtime 的最大值**，不是目录自身 mtime ——
  Linux 目录 mtime 只在增删条目时更新，持续往里写 raw.pcm 是不会变的，
  拿它当判据会把正在录的那场当成「很久没动」。

默认 dry-run，只报告不动文件；加 --apply 才真的执行。

    python3 deploy/retention.py                        # 看看会删什么
    python3 deploy/retention.py --apply                # 执行
    python3 deploy/retention.py --days 90 --trash-days 14 --apply
    python3 deploy/retention.py --live-days 0          # 只清归档，不动 live
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".aac"}
FAVORITE_NAME = ".favorite"
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

LIVE_DIRNAME = "live"
LIVE_TRASH_PREFIX = "live-"
DEFAULT_HEALTH_URL = "http://127.0.0.1:8848/api/health"


def archive_date(folder: Path) -> tuple[datetime, str]:
    """归档的「会议日期」。

    优先用目录名里的日期（2026-01-01_会议_0930）—— 它代表这场会议是哪天开的，
    换机器、拷文件都不会变。目录名没有日期（早期测试归档）才退回用 mtime。
    """
    m = DATE_RE.match(folder.name)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))), "目录名"
        except ValueError:
            pass
    return datetime.fromtimestamp(folder.stat().st_mtime), "文件时间"


def iter_archives(data_root: Path):
    """产出 (owner, 归档目录)。多租户在 tenants/<owner>/processed 下，
    早期的单租户归档直接在 data/processed 下，两种都要照顾到。"""
    tenants = data_root / "tenants"
    if tenants.is_dir():
        for own_dir in sorted(tenants.iterdir()):
            if not own_dir.is_dir():
                continue
            proc = own_dir / "processed"
            if not proc.is_dir():
                continue
            for folder in sorted(proc.iterdir()):
                if folder.is_dir() and not folder.name.startswith("_"):
                    yield own_dir.name, folder
    legacy = data_root / "processed"
    if legacy.is_dir():
        for folder in sorted(legacy.iterdir()):
            if folder.is_dir() and not folder.name.startswith("_"):
                yield None, folder


def audio_files(folder: Path) -> list[Path]:
    return [p for p in sorted(folder.iterdir())
            if p.is_file() and p.suffix.lower() in AUDIO_EXTS]


def iter_live_dirs(data_root: Path):
    """产出 (owner, 会话工作目录)。

    真实布局是 tenants/<owner>/work/live/<会话id>/；
    早期的单租户布局 data/work/live/ 也照顾一下（现在多半是空壳，为空自动跳过）。
    """
    tenants = data_root / "tenants"
    if tenants.is_dir():
        for own_dir in sorted(tenants.iterdir()):
            if not own_dir.is_dir():
                continue
            live = own_dir / "work" / LIVE_DIRNAME
            if not live.is_dir():
                continue
            for d in sorted(live.iterdir()):
                if d.is_dir():
                    yield own_dir.name, d
    legacy = data_root / "work" / LIVE_DIRNAME
    if legacy.is_dir():
        for d in sorted(legacy.iterdir()):
            if d.is_dir():
                yield None, d


def last_activity(folder: Path) -> datetime:
    """目录的「最后活动时间」= 目录内所有文件 mtime 的最大值。

    不能只看目录自身 mtime：Linux 的目录 mtime 只在增删条目时更新，
    会话还在往里追加 raw.pcm 时它是不会变的 —— 拿它当判据，
    正在录的那一场会被当成「很久没动」直接搬走。
    """
    newest: float | None = None
    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if newest is None or m > newest:
            newest = m
    if newest is None:
        try:
            newest = folder.stat().st_mtime
        except OSError:
            newest = 0.0
    return datetime.fromtimestamp(newest)


def active_sessions(url: str, timeout: float = 2.0) -> int | None:
    """读 /api/health 的 sessions。连不上返回 None —— 那不代表「0 个会话」。

    live 目录里正在录的那一场，文件还在追加、mtime 就是「当下」，
    按天数判本来不会命中。这道保险是防时钟漂移 / NTP 校时把 mtime
    推到很久以前时误删正在录的场次：宁可这一轮不清理，也不赌。
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    n = data.get("sessions")
    return int(n) if isinstance(n, (int, float)) else None


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def move_to_trash(trash_root: Path, owner: str | None, folder: Path, files: list[Path],
                  dry: bool) -> None:
    """按「天 + 归档名」分层放进回收站，保留原来的目录名便于认出来。"""
    dest = trash_root / (owner or "_legacy") / datetime.now().strftime("%Y%m%d") / folder.name
    if dry:
        print(f"      → 会移到 {dest}")
        return
    dest.mkdir(parents=True, exist_ok=True)
    for f in files:
        shutil.move(str(f), str(dest / f.name))
    print(f"      → 已移到 {dest}")


def move_dir_to_trash(trash_root: Path, owner: str | None, folder: Path, dry: bool) -> None:
    """整个目录搬进回收站（live 工作区是临时区，整目录搬比逐个文件快且完整）。

    刻意加 `live-` 前缀：回收站按 <owner>/<YYYYMMDD>/ 分层，
    purge_trash 只认 <YYYYMMDD> 这一层的日期，里面怎么命名都不影响到期判断，
    但加前缀能让「这是临时区垃圾、不是归档音频」一眼可辨，也避免与同日归档撞名。
    """
    day = datetime.now().strftime("%Y%m%d")
    dest = trash_root / (owner or "_legacy") / day / f"{LIVE_TRASH_PREFIX}{folder.name}"
    if dry:
        print(f"      → 会移到 {dest}")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # 同一天同一 id 再搬一次（正常不会发生），加时间戳，绝不覆盖已搬走的
        dest = dest.with_name(f"{dest.name}-{datetime.now():%H%M%S}")
    shutil.move(str(folder), str(dest))
    print(f"      → 已移到 {dest}")


def purge_trash(trash_root: Path, keep_days: int, dry: bool) -> tuple[int, int]:
    """回收站里放超过 keep_days 天的批次，整批删掉。返回 (批次数, 释放字节)。"""
    if not trash_root.is_dir():
        return 0, 0
    cutoff = datetime.now() - timedelta(days=keep_days)
    batches = 0
    freed = 0
    for owner_dir in sorted(trash_root.iterdir()):
        if not owner_dir.is_dir():
            continue
        for batch in sorted(owner_dir.iterdir()):
            if not batch.is_dir():
                continue
            try:
                bdate = datetime.strptime(batch.name, "%Y%m%d")
            except ValueError:
                continue          # 不是我们建的批次目录，不碰
            if bdate >= cutoff:
                continue
            size = sum(p.stat().st_size for p in batch.rglob("*") if p.is_file())
            cnt = sum(1 for p in batch.rglob("*") if p.is_file())
            print(f"  回收站到期：{owner_dir.name}/{batch.name} "
                  f"（{cnt} 文件 / {human(size)}）")
            batches += 1
            freed += size
            if not dry:
                shutil.rmtree(batch)
    return batches, freed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="留存清理（归档音频 + live 工作区，默认 dry-run）")
    ap.add_argument("--data", default="~/data", help="数据根目录（默认 ~/data）")
    ap.add_argument("--days", type=int, default=60, help="归档音频保留天数（默认 60）")
    ap.add_argument("--live-days", type=float, default=7,
                    help="live 工作区保留天数（默认 7；可带小数，设 0 = 完全不碰 live）")
    ap.add_argument("--live-min-hours", type=float, default=24,
                    help="live 目录的硬下限：这么多小时内动过的一律不碰（默认 24）")
    ap.add_argument("--health-url", default=DEFAULT_HEALTH_URL,
                    help="查活动会话用（默认本机 8848）")
    ap.add_argument("--trash-days", type=int, default=7, help="回收站再留几天（默认 7）")
    ap.add_argument("--apply", action="store_true", help="真的执行（不加只报告）")
    args = ap.parse_args(argv)

    data_root = Path(args.data).expanduser()
    if not data_root.is_dir():
        print(f"[错误] 数据目录不存在：{data_root}", file=sys.stderr)
        return 2
    trash_root = data_root / "trash"
    dry = not args.apply
    cutoff = datetime.now() - timedelta(days=args.days)

    print(f"{'[预演] ' if dry else ''}归档音频保留 {args.days} 天，"
          f"回收站再留 {args.trash_days} 天，截止 {cutoff:%Y-%m-%d}")

    keep_star, keep_new, expired = 0, 0, 0
    freed = 0
    for owner, folder in iter_archives(data_root):
        files = audio_files(folder)
        if not files:
            keep_new += 1
            continue
        if (folder / FAVORITE_NAME).is_file():
            keep_star += 1
            print(f"  ★ 收藏跳过：{(owner + '/') if owner else ''}{folder.name}")
            continue
        when, src = archive_date(folder)
        if when >= cutoff:
            keep_new += 1
            continue
        size = sum(p.stat().st_size for p in files)
        expired += 1
        freed += size
        print(f"  到期：{(owner + '/') if owner else ''}{folder.name}"
              f"（{when:%Y-%m-%d}，按{src}；{len(files)} 个音频 / {human(size)}）")
        move_to_trash(trash_root, owner, folder, files, dry)

    # ---------- live 会话工作区 ----------
    live_keep = live_expired = 0
    live_freed = 0
    live_note = ""
    if args.live_days > 0:
        live_cutoff = datetime.now() - timedelta(days=args.live_days)
        min_age = datetime.now() - timedelta(hours=args.live_min_hours)
        n_sess = active_sessions(args.health_url)
        pairs = list(iter_live_dirs(data_root))
        print()
        print(f"{'[预演] ' if dry else ''}live 工作区保留 {args.live_days} 天"
              f"（硬下限 {args.live_min_hours:g} 小时），"
              f"共 {len(pairs)} 个目录")
        if n_sess:
            live_keep = len(pairs)
            live_note = f"有 {n_sess} 个会话正在跑，本轮跳过 live"
            print(f"  ⏸ {live_note}")
        elif n_sess is None:
            live_note = "读不到 /api/health（服务没起或地址不通），改用时间双保险"
            print(f"  ! {live_note}")

        if not n_sess:
            for owner, d in pairs:
                when = last_activity(d)
                label = f"{(owner + '/') if owner else ''}{d.name}"
                if when >= min_age or when >= live_cutoff:
                    live_keep += 1
                    continue
                size = sum(p.stat().st_size for p in d.rglob("*") if p.is_file())
                live_expired += 1
                live_freed += size
                print(f"  到期：{label}（最后写入 {when:%Y-%m-%d %H:%M}；{human(size)}）")
                move_dir_to_trash(trash_root, owner, d, dry)

    batches, freed_trash = purge_trash(trash_root, args.trash_days, dry)

    print()
    print(f"{'[预演] ' if dry else ''}汇总：")
    print(f"  收藏豁免     {keep_star} 个归档")
    print(f"  未到期/无音频 {keep_new} 个归档")
    print(f"  归档到期清理 {expired} 个归档，{human(freed)}")
    if args.live_days > 0:
        print(f"  live 保留    {live_keep} 个目录")
        print(f"  live 清理    {live_expired} 个目录，{human(live_freed)}")
        if live_note:
            print(f"  live 备注    {live_note}")
    else:
        print("  live 清理    已停用（--live-days 0）")
    print(f"  回收站清空   {batches} 个批次，{human(freed_trash)}")
    if dry and (expired or batches or live_expired):
        print("\n以上为预演。确认无误后加 --apply 执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
