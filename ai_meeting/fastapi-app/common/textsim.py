"""条目身份判据：判断两段文本是否"说的同一件事"。

抽出来的理由与 `timex.py` 相同：这个判据同时被产品（判断条目是否被静默删除）和
评测套件（配对初稿与修订稿、判断是否合并）使用。两处各写一份必然漂移——之前
评测里已经踩过：纯用文本相等会把"改写"误判成"删除"，192 条里绝大部分是改写；
纯用 4-gram 覆盖率又会把"合并"漏判。所以判据只在**一处**定义，两侧共用。

判据本身是两条的组合，缺一不可：
1. 相似度比 ≥ 0.5（SequenceMatcher，对改写与压缩宽容）；
2. 最长公共块 ≥ min(4, 较短文本长度)。

**两条判据的实测状态不同，引用时不要混为一谈**（2026-09-25 在 test26(26 场)、
dev22(22 场)、合成集(6 场) 三批真实产物上复核）：

* **第 1 条吃重。** 相似度落在 [0.45, 0.55] 的候选对每批有 20–23 对；把阈值从 0.4
  扫到 0.7，下游的 `silent_deletion` 只从 71 变到 74（test26）、93 变到 99（dev22），
  即结论方向与量级都不随阈值摆动，**在 test26 上校准的 0.5 可以直接用于 dev22**。
* **第 2 条目前不绑定。** 把 `MIN_MATCHED_BLOCK` 降到 1（等于取消该判据），
  三批数据的配对结果与 `silent_deletion` **一个都没变**；检索 ratio ≥ 0.5 且最长公共块
  < 4 的候选对，三批都是 **0 对**。也就是说下例那种形态在现有数据里没有出现过：
  `旧事项` vs `全新的事项` 相似度恰好 0.50、最长公共块 2 —— 它是构造出来的边界形态，
  不是观测到的假配对。

因此这条判据保留为**对已知失效形态的廉价护栏**（改动它不影响当前结论），
但文档不再声称它来自观测。复核脚本：见 `evaluation/metrics/README.md` 的
「阈值复核」一节。

`is_covered_by` 的 0.6 覆盖阈值在 2026-09-26 之前没有针对它所在路径
（`matches_any` → 保默认恢复 / `silent_deletion` / 评测配对）的敏感性记录，
审计把它列为"最像可调分旋钮的位置"。当日在契约 v2 的 26 场产物上补扫
0.4→1.0（`evaluation/metrics/threshold_sensitivity.py`，零模型调用）：
**`silent_deletion` 在全区间恒为 80、"仍在"率恒为 92.92%——该判据在当前语料
上从未改变任何结局**（保留/删除全部由子串包含与相似度 ≥0.5 驱动）。
结论：当前无调分风险，但它与 `MIN_MATCHED_BLOCK` 一样是**惰性护栏**而非
经校准的判据；若产物风格变化（例如大量合并式改写）它可能被激活，
改它之前必须重跑该扫描。
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

SIMILARITY_THRESHOLD = 0.5
MIN_MATCHED_BLOCK = 4
DEFAULT_GRAM_SIZE = 4

_CLAUSE_SPLIT = re.compile(r"[。！？；\n，、,;!?]+")


def normalize(text: object) -> str:
    """NFC，保留字母与数字，去掉空白与标点，ASCII 小写化。"""
    if not isinstance(text, str):
        return ""
    return "".join(
        char for char in unicodedata.normalize("NFC", text)
        if unicodedata.category(char)[0] in "LN"
    ).casefold()


def similarity(first: str, second: str) -> tuple[float, int]:
    """返回（相似度比，最长公共块长度）。"""
    if not first or not second:
        return 0.0, 0
    if first == second:
        return 1.0, len(first)
    matcher = SequenceMatcher(None, first, second)
    longest = max((block.size for block in matcher.get_matching_blocks()), default=0)
    return matcher.ratio(), longest


def is_same_item(first: str, second: str) -> bool:
    """共享的条目身份判据。产品与评测必须调用这一个，不要各自实现。"""
    ratio, longest = similarity(first, second)
    if ratio < SIMILARITY_THRESHOLD:
        return False
    return longest >= min(MIN_MATCHED_BLOCK, len(first), len(second))


def content_grams(text: str, *, size: int = DEFAULT_GRAM_SIZE) -> set[str]:
    """按标点切小句后取字符 n-gram 集合；跨标点的 n-gram 不算任何人的断言。"""
    grams: set[str] = set()
    for clause in _CLAUSE_SPLIT.split(text or ""):
        normalized = normalize(clause)
        if len(normalized) < size:
            continue
        for start in range(len(normalized) - size + 1):
            grams.add(normalized[start:start + size])
    return grams


def coverage(grams: set[str], pool: set[str]) -> float:
    """这些措辞有多少比例出现在候选池里。"""
    if not grams:
        return 0.0
    return sum(1 for gram in grams if gram in pool) / len(grams)


def is_covered_by(grams: set[str], pool: set[str], *, threshold: float = 0.6) -> bool:
    """措辞是否基本都被候选池覆盖——用于区分"合并"与"删除"。"""
    return coverage(grams, pool) >= threshold


def matches_any(text: str, candidates: list[str]) -> bool:
    """文本是否与候选中的任意一条是同一件事。

    三条判据按代价从低到高：
    1. **包含**：短条目取不出 n-gram，但"乙事项"明显在"关于乙事项的进一步说明"里，
       这是展开/拆分的常见形态，必须算"还在"；
    2. **身份判据**（相似度 + 最长公共块）：容忍改写；
    3. **措辞覆盖**：容忍合并——原条目的措辞被吸收进别的条目。
    """
    if not text:
        return False
    normalized = normalize(text)
    grams = content_grams(text)
    for candidate in candidates:
        if not candidate:
            continue
        candidate_norm = normalize(candidate)
        if normalized and (normalized in candidate_norm or candidate_norm in normalized):
            return True
        if is_same_item(normalized, candidate_norm):
            return True
        if grams and is_covered_by(grams, content_grams(candidate)):
            return True
    return False
