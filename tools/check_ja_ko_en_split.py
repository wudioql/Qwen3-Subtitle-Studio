"""日/韩整段 → 逐句切出英文：**效果判据**（非一次性诊断探针）。

与本目录另两个工具的区别：`env_check_native_api.py` / `cloud_models_cli.py` 是
**长期驻留的环境探针**（AGENTS.md 门禁点名）；本脚本是某条具体判据的回归判据，
判据改动后可重跑确认效果未退化——因此归在这里而非 tests/。

覆盖 42 项矩阵：
- 目标能力：日整段 / 韩整段下的纯英文句 → ``English``
- 保护面：中/粤既有行为**逐字不变**（粤语尤其不能被误标为中文或英文）
- 边界：日语句含Kanji（与汉字同码区），不得被拉丁字母带跑成English

零模型加载，纯文本层。用法：

    ./.venv/Scripts/python.exe tools/check_ja_ko_en_split.py

期望输出：「错判 0 项」。断言等价物已固化在
``tests/test_cloud_asr.py::test_patch_splits_english_out_of_ja_ko_segment``
与 ``::test_patch_ja_ko_branch_never_touches_cantonese_behavior``。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.cloud_asr.language import split_zh_en_sentence_language

SENSEVOICE = "FunAudioLLM/SenseVoiceSmall"
QWEN3 = "Qwen/Qwen3-ASR-1.7B"   # noqa: F841  (保留：将来本地模型实测时用)

# (标签, 单句文本, 该句真值语种)
CASES = [
    # ── 日语整段里的英文句：目标新增能力 ──
    ("日→英 短", "This is a Japanese song.", "English"),
    ("日→英 长", "Politics and the English language from Wikipedia.", "English"),
    ("日→英 带标点尾巴", "I learned it from the internet.", "English"),
    ("日→英 带数字", "Track 3 is my favorite one.", "English"),
    # ── 韩语整段里的英文句：目标新增能力 ──
    ("韩→英 短", "This is a K-pop song.", "English"),
    ("韩→英 长", "Politics and the English language from Wikipedia.", "English"),
    ("韩→英 带标点尾巴", "I learned it from the internet.", "English"),
    # ── 必须沿用整段的日语/韩语句子：保护面 ──
    ("日 纯日文", "桜の花が咲きました", "Japanese"),
    ("日 汉字+假名", "日本語勉強中", "Japanese"),
    ("韩 纯韩文", "꽃이 피었습니다", "Korean"),
    ("韩 谚文夹空格", "안녕하세요 여러분", "Korean"),
    # ── 中/粤既有行为：回归面（必须一字不变）──
    ("中→英", "Politics and the English language.", "English"),
    ("粤→英", "Politics and the English language.", "English"),
    ("粤语保护", "我嘅日本語好正。", "Cantonese"),
    ("中文保护", "这段是中文，应该沿用整段语种。", "Chinese"),
]


def run(label: str, verbose: bool = True) -> tuple[int, int, list[str]]:
    """对 (日/韩/中/粤) 四种整段语种跑一遍，统计命中与错判。"""
    total = 0
    wrong: list[str] = []
    for case_label, text, want in CASES:
        for proj in ("Japanese", "Korean", "Chinese", "Cantonese"):
            # 只在「真值语种 == 整段语种」或「真值是 English」时才有意义
            relevant = want in (proj, "English")
            if not relevant:
                continue
            got = split_zh_en_sentence_language(text, proj, SENSEVOICE) or proj
            total += 1
            if got != want:
                wrong.append(f"{case_label} [整段={proj}] 得到 {got}，应为 {want}")
            if verbose and got == want:
                print(f"    ✓ {case_label:16s} [{proj:9s}] → {got}")
    return total, len(wrong), wrong


print("=" * 74)
print("探针：日/韩 → 英（以及中/粤回归面）")
print("=" * 74)
total, n_wrong, wrong = run("current")
print(f"\n当前实现：{total} 项，错判 {n_wrong} 项")
for w in wrong:
    print(f"  ✗ {w}")

# ── 中/粤回归面单独确认：这部分必须一字不变 ──
print("\n" + "-" * 74)
print("中/粤回归面（必须与当前完全一致）")
print("-" * 74)
regress = [
    ("Politics and the English language.", "Chinese", "English"),
    ("Politics and the English language.", "Cantonese", "English"),
    ("我嘅日本語好正。", "Cantonese", "Cantonese"),
    ("这段是中文，应该沿用整段语种。", "Chinese", "Chinese"),
    ("桜の花が咲きました", "Japanese", "Japanese"),
    ("꽃이 피었습니다", "Korean", "Korean"),
]
for text, proj, want in regress:
    got = split_zh_en_sentence_language(text, proj, SENSEVOICE) or proj
    flag = "✓" if got == want else "✗"
    print(f"  {flag} [{proj:9s}] {text[:28]:30s} → {got}")

print()
print("注：修法落地后日/韩整段下的英文句应全部命中 English；")
print("    若这里出现 None，说明 _JA_KO_TO_EN 分支没走到。")
