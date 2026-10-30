"""单场会议问答的检索：不引入向量库，用中文 bigram + 英文词做轻量相关性排序。"""

from __future__ import annotations

import re

# 中文停用词（只保留有信息量的词）
_STOPWORDS = {
    "的", "了", "是", "在", "和", "与", "及", "或", "对", "于", "关于", "我们", "你们", "他们",
    "这个", "那个", "就是", "可以", "一个", "没有", "然后", "因为", "所以", "但是", "而且",
    "以及", "还有", "进行", "通过", "需要", "什么", "怎么", "为什么", "哪些", "哪个", "多少",
    "是否", "有没有", "一下", "已经", "还是", "如果", "这样", "那样", "现在", "会议", "纪要",
}

_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_ASCII_RE = re.compile(r"[a-zA-Z0-9_]+")
_PUNCT_RE = re.compile(r"[，。！？、；：,.!?;:\"'“”‘’()（）\[\]【】<>《》—\-…·]")

QA_TOP_K = 6
QA_MAX_CONTEXT_CHARS = 6000


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    text = str(text).lower()
    tokens: set[str] = set()
    for chunk in _CJK_RE.findall(text):
        if len(chunk) == 1:
            tokens.add(chunk)
            continue
        for index in range(len(chunk) - 1):
            bigram = chunk[index:index + 2]
            if bigram not in _STOPWORDS:
                tokens.add(bigram)
    for word in _ASCII_RE.findall(text):
        if len(word) >= 2 and word not in _STOPWORDS:
            tokens.add(word)
    return tokens


def score_question(question: str, content: str) -> float:
    """问题与片段的相关性分数；0 表示不相关。"""
    question_tokens = _tokens(question)
    content_tokens = _tokens(content)
    if not question_tokens or not content_tokens:
        return 0.0
    overlap = question_tokens & content_tokens
    if not overlap:
        return 0.0
    # 多字词命中权重更高；再用问题 token 数归一，避免长问题天然占优
    score = sum(1.0 + max(0, len(token) - 2) * 0.3 for token in overlap)
    score /= len(question_tokens) ** 0.5
    normalized_question = _PUNCT_RE.sub("", question.lower())
    normalized_content = _PUNCT_RE.sub("", content.lower())
    if len(normalized_question) >= 4 and normalized_question in normalized_content:
        score += 2.0
    return round(score, 3)


def rank_segments(question: str, segments: list[dict], top_k: int = QA_TOP_K) -> list[dict]:
    """按相关性返回 top-k 片段（分数为 0 的不返回）。"""
    scored = []
    for segment in segments:
        score = score_question(question, segment.get("content") or "")
        if score > 0:
            scored.append((score, segment))
    scored.sort(key=lambda item: (-item[0], item[1]["segment_no"]))
    return [segment for _, segment in scored[:top_k]]


def format_time(start_ms: int | None) -> str:
    total_seconds = max(0, int((start_ms or 0) / 1000))
    return f"{total_seconds // 60:02d}:{total_seconds % 60:02d}"


def build_context(segments: list[dict], max_chars: int = QA_MAX_CONTEXT_CHARS) -> str:
    """把片段拼成模型上下文，每行带上 segment_no/时间/说话人。"""
    lines = []
    size = 0
    for segment in segments:
        line = (
            f"[{segment['segment_no']}][{format_time(segment.get('start_ms'))}]"
            f"[{segment.get('speaker') or '未知'}] {segment.get('content') or ''}"
        )
        if size + len(line) > max_chars:
            break
        lines.append(line)
        size += len(line)
    return "\n".join(lines)
