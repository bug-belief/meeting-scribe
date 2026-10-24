#!/usr/bin/env python3
"""P2-7 回归锁：samples/ 的放行规则必须精确。

这块最容易出的两类事故，都不会报错，只会**默默**发生：

1. **真实录音被提交** —— `.gitignore` 写成 `samples/*` + `!samples/*.wav` 时，
   任何人把一段真人对话丢进去，`git add .` 就带走了。
2. **放行规则整体失效** —— 写成 `samples/`（带斜杠）再跟 `!samples/xxx`，
   git 不会进入被忽略的目录，后面所有 `!` 全部无效。
   2026-10-23 实测：5 个本该放行的文件**全被判为已忽略**。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOTAL = 0
FAILS = 0


def check(name: str, got, want) -> None:
    global TOTAL, FAILS
    TOTAL += 1
    ok = got == want
    if not ok:
        FAILS += 1
    print(f"  {'OK  ' if ok else 'FAIL'} {name}: 得={got!r} 期={want!r}")


def ignored(rel: str) -> bool:
    """真问 git：这个相对路径会不会被忽略。文件不必存在，git 只看规则。"""
    r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=ROOT)
    return r.returncode == 0


def main() -> int:
    print("1) 必须放行：生成脚本、说明文档、合成 WAV")
    for rel in ["samples/README.md", "samples/demo-voice.md",
                "samples/make_samples.py", "samples/demo-dual.wav",
                "samples/demo-mono.wav"]:
        check(f"{rel} 不被忽略", ignored(rel), False)

    print("\n2) 必须忽略：真实录音、TTS 人声、任意其他格式")
    for rel in ["samples/我家的录音.m4a", "samples/take1.wav",
                "samples/demo-speech-a.mp3", "samples/sub/deep.wav",
                "samples/anything.mp4"]:
        check(f"{rel} 被忽略", ignored(rel), True)

    print("\n3) 反向对照：临时把规则改成 samples/（带斜杠），放行必须失效")
    # 这是本次真正踩过的坑：目录被整体忽略时 git 不进去看，`!` 全部无效。
    # 用一个临时的 GIT_DIR 副本改规则来验证，不碰仓库自己的 .gitignore。
    import shutil
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="gitignore-probe-"))
    try:
        src = (ROOT / ".gitignore").read_text(encoding="utf-8")
        # 把正确的 `samples/**` 换回错误的 `samples/`
        broken = src.replace("samples/**\n", "samples/\n", 1)
        check("确实改到了规则（否则对照无效）", broken != src, True)
        (tmp / ".gitignore").write_text(broken, encoding="utf-8")
        # 造一个最小仓库：只需 .gitignore 和 git 元数据
        subprocess.run(["git", "init", "-q", str(tmp)], check=True)
        (tmp / "samples").mkdir(exist_ok=True)
        r = subprocess.run(["git", "check-ignore", "-q", "samples/README.md"], cwd=tmp)
        check("错误写法下 README.md 被判为忽略（证明正则是必要的）", r.returncode == 0, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n4) 产物存在性与基本形态")
    wav = ROOT / "samples" / "demo-dual.wav"
    check("demo-dual.wav 存在", wav.is_file(), True)
    if wav.is_file():
        import wave

        with wave.open(str(wav), "rb") as fh:
            ch, rate, width, frames = (fh.getnchannels(), fh.getframerate(),
                                       fh.getsampwidth(), fh.getnframes())
        check("立体声（2 声道）", ch, 2)
        check("采样率 16k（与 live_engine 一致）", rate, 16000)
        check("16 bit", width * 8, 16)
        # 关键：要够长，否则 peer_channel_alive 永远判不出「对方存在」
        check("时长 ≥ 8 秒（peer 观察门槛）", frames / rate >= 8.0, True)

    mo = ROOT / "samples" / "demo-mono.wav"
    check("demo-mono.wav 存在", mo.is_file(), True)
    if mo.is_file():
        import wave

        with wave.open(str(mo), "rb") as fh:
            check("单声道", fh.getnchannels(), 1)

    print("\n" + "-" * 46)
    print(f"通过 {TOTAL - FAILS} 项，失败 {FAILS} 项")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
