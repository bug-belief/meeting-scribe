"""生成 Android 各密度 launcher 图标。

绘制逻辑直接复用 web/make_icons.py 的 make()——网页图标和 App 图标必须是同一张图，
各画一套迟早会不一致。
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "web"))

from make_icons import make  # noqa: E402

# Android 会按屏幕密度挑最接近的一张；只给一套的话低端机会糊
DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}


def main() -> None:
    total = 0
    for dpi, size in DENSITIES.items():
        out = HERE / "res" / f"mipmap-{dpi}"
        out.mkdir(parents=True, exist_ok=True)
        path = out / "ic_launcher.png"
        make(size).save(path, optimize=True)
        total += path.stat().st_size
        print(f"  mipmap-{dpi:8} {size:>3}x{size:<3} {path.stat().st_size:>6} B")
    print(f"共 {len(DENSITIES)} 张，{total} B")


if __name__ == "__main__":
    print("生成 Android launcher 图标…")
    main()
