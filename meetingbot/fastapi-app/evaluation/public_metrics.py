"""公开评测集的通用指标（纯函数，不依赖数据库/模型）。"""

from __future__ import annotations

import re
from collections import Counter

from evaluation.metrics import normalize_text

_ASCII_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")


def prf(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def span_prf(gold: set[int], predicted: set[int]) -> dict:
    return prf(len(gold & predicted), len(predicted - gold), len(gold - predicted))


def _char_ngrams(text: str, size: int) -> Counter:
    normalized = normalize_text(text)
    if size <= 0:
        return Counter()
    return Counter(normalized[index:index + size] for index in range(max(0, len(normalized) - size + 1)))


def _ngrams(tokens: list[str], size: int) -> Counter:
    if size <= 0:
        return Counter()
    return Counter(tuple(tokens[index:index + size]) for index in range(max(0, len(tokens) - size + 1)))


def _f1(candidate: Counter, reference: Counter) -> float:
    overlap = sum((candidate & reference).values())
    candidate_total = sum(candidate.values())
    reference_total = sum(reference.values())
    if not candidate_total or not reference_total:
        return 0.0
    precision = overlap / candidate_total
    recall = overlap / reference_total
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def _lcs_length(left: list[str], right: list[str]) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = [0] * (len(right) + 1)
    for token in left:
        current = [0]
        for index, other in enumerate(right, start=1):
            if token == other:
                current.append(previous[index - 1] + 1)
            else:
                current.append(max(previous[index], current[-1]))
        previous = current
    return previous[-1]


def tokenize_text(text: str, tokenizer: str = "char") -> list[str]:
    """char：字符级（快速近似）；jieba：中文词级（可选，需安装 jieba）。"""
    if tokenizer == "jieba":
        try:
            import jieba
        except ImportError:
            tokenizer = "char"
        else:
            return [token.strip() for token in jieba.lcut(str(text or "")) if token.strip()]
    return list(normalize_text(text))


def token_rouge(candidate: str, reference: str, tokenizer: str = "char") -> dict:
    """token 级 ROUGE-1/2/L；默认 char，jieba 模式用于与中文分词论文对齐。"""
    candidate_tokens = tokenize_text(candidate, tokenizer)
    reference_tokens = tokenize_text(reference, tokenizer)
    if not candidate_tokens or not reference_tokens:
        return {"rouge1": 0.0, "rouge2": 0.0, "rougeL": 0.0}
    unigram = _f1(_ngrams(candidate_tokens, 1), _ngrams(reference_tokens, 1))
    bigram = _f1(_ngrams(candidate_tokens, 2), _ngrams(reference_tokens, 2))
    lcs = _lcs_length(candidate_tokens[:3000], reference_tokens[:3000])
    lcs_precision = lcs / len(candidate_tokens) if candidate_tokens else 0.0
    lcs_recall = lcs / len(reference_tokens) if reference_tokens else 0.0
    lcs_f1 = 2 * lcs_precision * lcs_recall / (lcs_precision + lcs_recall) if lcs_precision + lcs_recall else 0.0
    return {"rouge1": round(unigram, 4), "rouge2": round(bigram, 4), "rougeL": round(lcs_f1, 4)}


def char_rouge(candidate: str, reference: str) -> dict:
    """字符级 ROUGE-1/2/L，适用于中英文混合会议摘要的快速近似。"""
    return token_rouge(candidate, reference, tokenizer="char")


def tokens(text: str) -> set[str]:
    """中英文混合的轻量 token：英文/数字词 + 中文双字。"""
    raw = str(text or "")
    result = {word.lower() for word in _ASCII_WORD_RE.findall(raw)}
    normalized = normalize_text(raw)
    chinese = re.sub(r"[a-zA-Z0-9_]", "", normalized)
    result.update(chinese[index:index + 2] for index in range(max(0, len(chinese) - 1)))
    return result


def lexical_overlap_score(query: str, content: str) -> float:
    query_tokens = tokens(query)
    if not query_tokens:
        return 0.0
    content_tokens = tokens(content)
    return len(query_tokens & content_tokens) / len(query_tokens)


def answer_similarity(candidate: str, reference: str) -> float:
    """QA 答案的轻量相似度；正式评测应叠加人工/LLM rubric。"""
    from evaluation.metrics import text_similarity

    return round(text_similarity(candidate, reference), 4)


def average_metric(rows: list[dict], key: str) -> float:
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return round(sum(values) / len(values), 4) if values else 0.0
