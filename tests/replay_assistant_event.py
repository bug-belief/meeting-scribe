"""离线验证：给归档补一条播报事件，看那句是否被改回「助理」。

场景还原：用户点「试听」，合成音从 CABLE 播出去，被系统声卡抓进右声道，
原来的逻辑会把它当成「对方」。正确行为是按播报时间线改成「助理（我的声音）」。
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import cli_settings  # noqa: E402
from transcribe import transcribe_file  # noqa: E402

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else (
    Path.home() / "data/tenants/me/processed/2026-01-01_会议_0900/会议_0900.wav")
OUT = Path("/tmp/e1345")

EVENTS = [
    {
        "id": "test1",
        "kind": "tts",
        "text": "喂，能听到吗？我是你的声音分身在说话。",
        "start": 15.4,
        "end": 18.8,
        "spoken_chars": 19,
        "state": "done",
    },
    {
        "id": "u1",
        "kind": "tts",
        "text": "哎，你说。",
        "start": 7.8,
        "end": 9.3,
        "spoken_chars": 5,
        "state": "done",
    },
]


def main() -> int:
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    transcribe_file(SRC, OUT, cli_settings(), dual=True, events=EVENTS)
    md = (OUT / "transcript.md").read_text(encoding="utf-8")
    print(md[:1500])

    ok = True
    checks = [
        ("16 秒那句改成助理", "助理（我的声音）**：哎，能听到吗" in md),
        ("不再出现「对方」", "**对方**" not in md),
        ("8 秒那句仍是助理", "哎，你说" in md),
    ]
    for name, passed in checks:
        print(("  ✅ " if passed else "  ❌ ") + name)
        ok = ok and passed
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
