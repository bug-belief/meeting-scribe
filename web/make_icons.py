"""生成 PWA / APK 用的应用图标（纯本地生成，不引入新依赖）。

设计：深蓝圆角底 + 白色声波五条。声波比"麦克风"更适合这个应用——
它同时做转写和语音分身，麦克风图标太容易和录音笔混在一起。
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent
BLUE = (37, 99, 235, 255)      # #2563eb，和页面 --accent 一致
WHITE = (255, 255, 255, 255)
SS = 4                          # 超采样倍数，降采样后边缘才干净


def make(size: int, maskable: bool = False) -> Image.Image:
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # maskable 图标会被系统裁成圆形/水滴形，所以底色铺满、内容缩进安全区；
    # 普通图标自己带圆角，贴在桌面上不会显得方头方脑。
    if maskable:
        d.rectangle([0, 0, S - 1, S - 1], fill=BLUE)
        scale = 0.74
    else:
        pad = int(S * 0.015)
        d.rounded_rectangle([pad, pad, S - 1 - pad, S - 1 - pad],
                            radius=int(S * 0.22), fill=BLUE)
        scale = 1.0

    heights = (0.30, 0.50, 0.70, 0.50, 0.30)
    bar_w = S * 0.062 * scale
    gap = S * 0.052 * scale
    total = len(heights) * bar_w + (len(heights) - 1) * gap
    x = (S - total) / 2
    for h in heights:
        bh = S * h * scale
        y0 = (S - bh) / 2
        d.rounded_rectangle([x, y0, x + bar_w, y0 + bh],
                            radius=bar_w / 2, fill=WHITE)
        x += bar_w + gap

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    jobs = [
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-maskable-512.png", 512, True),
        ("apple-touch-icon.png", 180, False),
        ("favicon-32.png", 32, False),
    ]
    for name, size, maskable in jobs:
        icon = make(size, maskable)
        icon.save(OUT / name, optimize=True)
        print(f"  {name:24} {size}x{size}  {(OUT / name).stat().st_size:>6} B"
              f"{'  maskable' if maskable else ''}")


if __name__ == "__main__":
    print("生成图标…")
    main()
