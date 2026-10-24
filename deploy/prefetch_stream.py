"""预下载流式 ASR 模型（paraformer-zh-streaming），供 server.py 实时字幕使用。

    .venv/bin/python deploy/prefetch_stream.py

约 220 MB，下到 $MODELSCOPE_CACHE。只在首次部署时需要跑。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import SETTINGS  # noqa: E402


def main() -> int:
    print("模型缓存目录：", os.environ.get("MODELSCOPE_CACHE"))
    print("设备：", SETTINGS.asr_device)
    from funasr import AutoModel

    model = AutoModel(
        model="paraformer-zh-streaming",
        device="cpu",
        hub=SETTINGS.funasr_hub,
        disable_update=True,
    )
    print("流式模型就绪：", type(model).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
