"""离线自检：不装 funasr / 不联网，验证分块、渲染、去重状态机等纯逻辑。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from summarize import load_transcript, split_chunks  # noqa: E402
from transcribe import Segment, Transcript, merge_turns, render_markdown  # noqa: E402
from utils import estimate_tokens, is_temp_name, safe_stem  # noqa: E402

out = Path(__file__).parent
failures = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""))
    if not cond:
        failures.append(name)


# 1) 文本合并
segs = [
    Segment(0.0, 2.0, 0, "大家好，"),
    Segment(2.1, 4.0, 0, "开始今天的会议。"),
    Segment(4.1, 6.0, 1, "我先同步进度。"),
    Segment(30.0, 32.0, 0, "隔了很久的一句。"),
]
merged = merge_turns(segs)
check("同说话人短间隔合并", len(merged) == 3, f"got {len(merged)}")
check("合并后文本正确", merged[0].text == "大家好，开始今天的会议。", merged[0].text)
check("长间隔不合并", merged[-1].speaker == 0 and merged[-1].start == 30.0)

# 2) Markdown 渲染
tr = Transcript(source=Path("会议.m4a"), duration=3725, backend="funasr", segments=merged, notes=["测试提示"])
md = render_markdown(tr, "周会", "Speaker")
check("含时间戳", "[00:00:30]" in md, md.splitlines()[13] if len(md.splitlines()) > 13 else "")
check("含说话人标签", "Speaker 1**：大家好" in md and "Speaker 2**：" in md)
check("front-matter 时长", "duration: 01:02:05" in md)
check("speaker 数", "speakers: 2" in md)

# 3) 分块
line = "**[00:00:01] Speaker 1**：这是一段用于测试分块逻辑的会议发言内容，包含标点，长度约四十个字符左右。"
body = "\n".join(line for _ in range(400))
chunks = split_chunks(body, 6000)
check("分块数量合理", len(chunks) >= 2, f"chunks={len(chunks)}")
check("单块不超限", all(len(c) <= 6200 for c in chunks), f"max={max(len(c) for c in chunks)}")
check("分块不丢内容", sum(len(c.replace("\n", "")) for c in chunks) == len(body.replace("\n", "")))

# 4) 转写稿解析 + 长稿判定
long_md = out / "transcript_long.md"
long_md.write_text(
    "---\nsource: 会议.m4a\ntitle: 季度经营会\nduration: 00:45:00\nspeakers: 4\nbackend: funasr\n---\n\n"
    + body,
    encoding="utf-8",
)
doc = load_transcript(long_md)
check("front-matter 解析", doc.meta.get("duration") == "00:45:00" and doc.title == "季度经营会")
check("正文剥离", not doc.body.startswith("---"))
check("长稿走分块", len(split_chunks(doc.body, 6000)) > 1)
print(f"      长稿 {len(doc.body)} 字 ≈ {estimate_tokens(doc.body)} tokens")

# 5) 工具函数
check("临时文件识别 .tmp", is_temp_name("a.m4a.tmp"))
check("临时文件识别 syncthing", is_temp_name(".syncthing.a.m4a.tmp"))
check("临时文件识别隐藏", is_temp_name(".DS_Store"))
check("正常文件不误判", not is_temp_name("2026-10-18 周会.m4a"))
check("文件名清洗", safe_stem('a/b:c*?"<>|') == "a_b_c", safe_stem('a/b:c*?"<>|'))

print("-" * 50)
print("FAILED:", failures if failures else "none")
sys.exit(1 if failures else 0)
