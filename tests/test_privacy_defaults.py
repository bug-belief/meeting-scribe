#!/usr/bin/env python3
"""P2-5 回归锁：不许在代码/配置里写死某人的地理位置。

为什么单独做一个测试文件：这类泄漏**不会让任何功能失败**，只会让公开仓
带着一个人的住址发出去。护栏必须是「仓库里出现该形态就红」，不能靠人记得查。

覆盖三层：
  ① 代码默认值  —— config.py 的 ASSISTANT_HOMETOWN 默认必须为空
  ② 提示词拼接  —— 没配家乡时，系统提示词里不得出现方言段（含占位符残留）
  ③ 全仓扫描    —— 跟踪范围内不得出现「省+市+县/镇/村」形态的地名
"""

from __future__ import annotations

import re
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


def sh(cmd: str) -> str:
    return subprocess.run(cmd, shell=True, cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace").stdout.strip()


def main() -> int:
    sys.path.insert(0, str(ROOT))

    print("1) 代码默认值：ASSISTANT_HOMETOWN 必须为空")
    cfg = (ROOT / "config.py").read_text(encoding="utf-8")
    m = re.search(r'assistant_hometown=_str\(\s*"ASSISTANT_HOMETOWN"\s*,\s*"([^"]*)"\s*\)', cfg)
    check("找到该配置项", bool(m), True)
    check("默认值为空串", m.group(1) if m else "<未找到>", "")

    print("\n2) 提示词：没配家乡时不得出现方言段")
    import assistant

    p_empty = assistant.build_system_prompt("")
    check("空家乡 → 无占位符残留", "__DIALECT_HINT__" in p_empty, False)
    check("空家乡 → 无「说话人来自」", "说话人来自" in p_empty, False)
    check("空家乡 → 无「家乡」字样", "家乡" in p_empty, False)
    # 基础提示词本身仍在（别为了脱敏把提示词弄坏）
    check("空家乡 → 保留核心身份句", "语音分身" in p_empty, True)
    check("空家乡 → 保留硬性要求", "硬性要求" in p_empty, True)

    p_set = assistant.build_system_prompt("浙江杭州")
    check("配了家乡 → 出现该地名", "浙江杭州" in p_set, True)
    check("配了家乡 → 方言段生效", "说话人来自浙江杭州" in p_set, True)
    check("配了家乡 → 仍无占位符残留", "__DIALECT_HINT__" in p_set, False)
    # 空白串要当作「没配」，否则会拼出「说话人来自  一带」
    p_blank = assistant.build_system_prompt("   ")
    check("纯空白 → 视为未配置", "说话人来自" in p_blank, False)

    print("\n3) 全仓扫描：跟踪范围内不得出现精确到县/镇/村的地名")
    # 形态：省(2-3字) + 市/州 + 县/区/市 + 镇/乡/村/街道
    # ⚠️ 只匹配「四级俱全」的写法 —— 匹配「省+市」会把正常示例也算进来（假阳性）
    exact = re.compile(r"[\u4e00-\u9fa5]{2,4}省[\u4e00-\u9fa5]{2,6}市[\u4e00-\u9fa5]{2,6}[县区市][\u4e00-\u9fa5]{1,6}[镇乡村]")
    hits = [ln for ln in sh("git -c core.quotepath=false grep -nE "
                            r"'[\u4e00-\u9fa5]{2,4}省[\u4e00-\u9fa5]{2,6}市' -- . "
                            "2>/dev/null").splitlines()
            if exact.search(ln)]
    check("四级地名命中数", len(hits), 0)
    for h in hits[:5]:
        print(f"       {h[:110]}")

    print("\n4) 反向对照：判据不是恒真")
    check("自检样本能命中", bool(exact.search("甲乙省丙丁市戊己县庚辛镇")), True)
    check("自检样本不误报省+市", bool(exact.search("浙江省杭州市")), False)
    check("自检样本不误报纯市名", bool(exact.search("浙江杭州")), False)

    print("\n5) 方言词表只提供模板，真实文件不入库")
    check("模板存在", (ROOT / "DIALECT.example.md").is_file(), True)
    check("DIALECT.md 未被跟踪", sh("git ls-files DIALECT.md"), "")
    check("DIALECT.md 在 gitignore 里", bool(sh("git check-ignore DIALECT.md")), True)
    tpl = (ROOT / "DIALECT.example.md").read_text(encoding="utf-8")
    check("模板不含真实地名", bool(exact.search(tpl)), False)
    check("模板说明了怎么用", "复制" in tpl, True)

    print("\n" + "-" * 46)
    print(f"通过 {TOTAL - FAILS} 项，失败 {FAILS} 项")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
