"""双路分轨命名自检：不加载模型，只验证「按声道判人」这段逻辑。

    .venv/bin/python tests/test_dual.py

左声道=麦克风（我）、右声道=系统声音（对方）。核心断言：
1. 两段互不重叠、分别落在左右声道的语音 → 两个簇各自命名
2. 同一侧出现两个簇 → 只给能量更强的那个命名，其余回退成 Speaker N
3. 文件读不了 / 只有单声道 → 返回 None，调用方保留声纹结果
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from transcribe import Segment, _apply_channel_names  # noqa: E402

SR = 16000
PASS = 0
FAIL = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  ✅ {name}" + (f"：{detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f"：{detail}" if detail else ""))


def make_stereo(path: Path, bursts: list[tuple[float, float, int, float]]) -> None:
    """bursts: (开始秒, 结束秒, 声道, 幅度)。用带包络的噪声近似语音，够判能量。"""
    end = max(b for _, b, _, _ in bursts) + 0.2
    data = np.zeros((int(end * SR), 2), dtype=np.float32)
    rng = np.random.default_rng(7)
    for start, stop, ch, amp in bursts:
        n = int((stop - start) * SR)
        env = np.hanning(n).astype(np.float32)
        data[int(start * SR):int(start * SR) + n, ch] += (
            rng.normal(0, amp, n).astype(np.float32) * env
        )
    sf.write(str(path), data, SR, subtype="PCM_16")


def seg(start: float, end: float, speaker: int) -> Segment:
    return Segment(start=start, end=end, speaker=speaker, text="x")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # 1) 左右各一段，互不重叠
        p1 = tmp_dir / "a.wav"
        make_stereo(p1, [(0.0, 2.0, 0, 0.2), (2.0, 4.0, 1, 0.2)])
        info: dict = {}
        note = _apply_channel_names(p1, [seg(0, 2, 0), seg(2, 4, 1)], info, "我", "对方")
        check("左右各一段 → 两个名字",
              info.get("names") == {"0": "我", "1": "对方"},
              f"names={info.get('names')}")
        check("写入 dual 模式与声道映射",
              info.get("mode") == "dual"
              and info.get("channel_map") == {"0": "left", "1": "right"},
              f"mode={info.get('mode')} map={info.get('channel_map')}")
        check("返回一句可写进 notes 的说明", bool(note) and "左声道" in (note or ""), note or "")

        # 2) 同侧两个簇：现在允许并存，依次命名（麦克风侧也可能是多个人）
        #    ⚠️ 2026-10-21 改：这条用例两个 burst 都在声道 0（左），右声道全程无声，
        #    正好命中「电话外放」场景 —— 对方的声音从麦克风进来。
        #    此时把第 2 人叫「我2」等于把对方说成自己，改为 Speaker 2。
        p2 = tmp_dir / "b.wav"
        make_stereo(p2, [(0.0, 2.0, 0, 0.25), (2.2, 3.0, 0, 0.03)])
        info2: dict = {}
        _apply_channel_names(p2, [seg(0, 2, 0), seg(2.2, 3, 1)], info2, "我", "对方")
        check("右声道静音 + 同侧多簇 → 第 2 人标 Speaker 2（不冒充「我」）",
              info2.get("names") == {"0": "我", "1": "Speaker 2"},
              f"names={info2.get('names')}")

        # 2b) 对照组：右声道确实有声音时，同侧多簇仍按「我2」编号
        p2b = tmp_dir / "b2.wav"
        make_stereo(p2b, [(0.0, 2.0, 0, 0.25), (2.2, 3.0, 0, 0.2),
                          (3.2, 6.0, 1, 0.2)])
        info2b: dict = {}
        _apply_channel_names(p2b, [seg(0, 2, 0), seg(2.2, 3, 1), seg(3.2, 6, 2)],
                             info2b, "我", "对方")
        check("右声道有效 + 同侧多簇 → 仍是「我、我2」",
              info2b.get("names") == {"0": "我", "1": "我2", "2": "对方"},
              f"names={info2b.get('names')}")

        # 3) 单声道文件（没有右声道）
        p3 = tmp_dir / "c.wav"
        mono = (np.random.default_rng(1).normal(0, 0.1, SR * 2)).astype(np.float32)
        sf.write(str(p3), mono, SR, subtype="PCM_16")
        info3: dict = {"names": {"0": "张三"}}
        note3 = _apply_channel_names(p3, [seg(0, 2, 0)], info3, "我", "对方")
        check("单声道 → 不覆盖声纹结果",
              note3 is None and info3.get("names") == {"0": "张三"},
              f"note={note3} names={info3.get('names')}")

        # 4) 文件不存在 → 安静失败
        info4: dict = {}
        note4 = _apply_channel_names(tmp_dir / "nope.wav", [seg(0, 1, 0)], info4, "我", "对方")
        check("文件读不了 → 返回 None 不抛异常", note4 is None, f"note={note4}")

        # 5) 双侧独立聚类：两侧各自编号，互不撞号
        from diarize import _cluster_stereo

        vecs = [
            np.array([1.0, 0.05], dtype="float32"),   # 左侧 A
            np.array([0.05, 1.0], dtype="float32"),   # 右侧 A
            np.array([0.98, 0.1], dtype="float32"),   # 左侧 A（同一个人）
            np.array([0.1, 0.97], dtype="float32"),   # 右侧 A（同一个人）
        ]
        labels = _cluster_stereo(vecs, ["left", "right", "left", "right"], 0.5, 4, 0.55)
        check("双侧分侧聚类，编号不撞车",
              labels[0] == labels[2] and labels[1] == labels[3]
              and labels[1] >= 1000 and labels[0] < 1000,
              f"labels={labels}")

    print(f"\n{'-' * 46}\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
