"""知识库的**词法臂**：把文本编成稀疏向量，交给 Qdrant 与稠密向量做 RRF 融合。

为什么需要词法臂：稠密向量擅长"意思相近"，但会漏掉**精确串**——编号、人名、
日期、专有名词（"V2.0""报表导出""MTG2026..."）。生产上的混合检索就是为了补这一块。

为什么是字符 bigram 而不是分词：中文分词要引依赖（jieba 等）且词典外的新词
（产品名、人名）本来就分不出来；bigram 对中文是公认可用的折中，且**零依赖**。

**诚实标注一处简化**：这里用的是次线性词频（`1 + log(tf)`）而不是完整 BM25——
BM25 的 IDF 需要全语料文档频率，而向量是在索引那一刻定死的，事后改不了。
对本项目的用途（补精确串召回）词频权重够用；若日后实测词法臂召回不足，
正确的升级路径是离线统计 `kb_chunk` 的 df 再重算权重，而不是在这里拍一个常数。

索引用 `zlib.crc32` 而不是内置 `hash()`：后者每进程随机加盐，跨进程不一致，
把索引写进向量库后查询就会查不到——这是最容易踩、又最难查的一类 bug。
"""

from __future__ import annotations

import math
import re
import zlib
from collections import Counter
from typing import Iterable

# 哈希空间：2^20 个槽位。够稀疏（冲突概率低），又远小于 Qdrant 允许的 uint32 上限
_HASH_SPACE = 1 << 20
# ASCII 词（英文单词 / 数字 / 带点的编号）按词切；其余非空白字符按字切
_ASCII_WORD = re.compile(r"[A-Za-z0-9_.\-]+")
_CJK_OR_OTHER = re.compile(r"[^\sA-Za-z0-9_.\-]")


def tokenize(text: str) -> list[str]:
    """把文本切成词法 token：ASCII 词 + 其余字符的相邻 bigram。"""
    if not text:
        return []
    tokens: list[str] = []
    for match in _ASCII_WORD.finditer(text):
        word = match.group(0).lower()
        if len(word) > 1:  # 单字母噪声大（中文文本里成片的单字母多来自误听）
            tokens.append(word)
    # 非 ASCII 部分按"连续非空白串"取 bigram，避免跨标点拼出无意义组合
    for span in re.split(r"[\sA-Za-z0-9_.\-]+", text):
        chars = _CJK_OR_OTHER.findall(span)
        if len(chars) == 1:
            tokens.append(chars[0])
        for first, second in zip(chars, chars[1:], strict=False):
            tokens.append(first + second)
    return tokens


def _index_of(token: str) -> int:
    return zlib.crc32(token.encode("utf-8")) % _HASH_SPACE


def encode(text: str) -> dict[str, list]:
    """把文本编成 Qdrant 稀疏向量 `{"indices": [...], "values": [...]}`。

    权重取次线性词频并做 L2 归一化：归一化让"长切片"不会仅因为长就压过短切片。
    """
    counts = Counter(tokenize(text))
    if not counts:
        return {"indices": [], "values": []}
    raw = {_index_of(token): 1.0 + math.log(count) for token, count in counts.items()}
    # 同一槽位可能被不同 token 哈希撞上：合并权重而不是互相覆盖
    merged: dict[int, float] = {}
    for index, weight in raw.items():
        merged[index] = merged.get(index, 0.0) + weight
    norm = math.sqrt(sum(w * w for w in merged.values())) or 1.0
    indices = sorted(merged)
    return {"indices": indices, "values": [round(merged[i] / norm, 6) for i in indices]}


def vocabulary_size(tokens: Iterable[str]) -> int:
    """被用到过的哈希槽位数——用于粗看稀疏度，不参与检索。"""
    return len({_index_of(t) for t in tokens})
