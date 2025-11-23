"""应用层 RRF：dense 与 BM25 两路融合。

两路检索都在 PostgreSQL 内（pgvector dense + pg_search BM25），但 SQL 仍分别
返回两路排名；这里在应用层做 RRF，避免把排序权重写进 SQL。
RRF 只看排名，不依赖两路分数量纲，适合这种跨索引融合。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FusedHit:
    chunk_id: int
    score: float


def rrf_fuse(
    rankings: list[list[int]],
    *,
    k: int = 60,
    limit: int = 24,
) -> list[FusedHit]:
    """按 RRF 融合多路 chunk_id 排名。

    `rankings` 每一路按相关性从高到低；同一个 chunk 在多路出现时分数相加。
    并列时按首次出现顺序稳定排序（避免结果在相同输入下抖动）。
    """
    scores: dict[int, float] = {}
    order: dict[int, int] = {}
    counter = 0
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            cid = int(chunk_id)
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
            if cid not in order:
                order[cid] = counter
                counter += 1
    fused = sorted(scores.items(), key=lambda item: (-item[1], order[item[0]]))
    return [FusedHit(chunk_id=cid, score=score) for cid, score in fused[:max(1, limit)]]


def fuse_dense_and_bm25(
    dense_ids: list[int],
    bm25_ids: list[int],
    *,
    k: int = 60,
    limit: int = 24,
) -> list[FusedHit]:
    return rrf_fuse([dense_ids, bm25_ids], k=k, limit=limit)


__all__ = ["FusedHit", "rrf_fuse", "fuse_dense_and_bm25"]
