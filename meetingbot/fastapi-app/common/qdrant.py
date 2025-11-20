"""Qdrant 访问层：向量库的读写与混合检索。

为什么用裸 httpx 而不是 `qdrant-client` SDK：本仓调百炼也是裸 httpx（见
transcription_service），保持一致；而且用到的只有四个 REST 接口，
引一个 SDK 换来的只是多一份依赖要跟版本。

为什么建 Qdrant 而不是"向量存 MySQL 暴力算"：MySQL 8.4 没有向量类型，
Community 版 9.x 也缺 ANN 索引（只能全表扫）；而这里既要按会议过滤又要混合检索，
Qdrant 的原生 payload 过滤 + 服务端 RRF 融合正好覆盖，且是 100K–10M 量级的主流选择。

**一处必须记住的约束**：`allowed_meeting_ids=None` 表示"不过滤"（管理员），
传空列表表示"没有任何可见会议"（必须返回空结果）。调用方绝不能用 None 表示后者——
那会变成越权。这个区分是本模块最容易出错的地方，测试里专门钉住了它。
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Sequence

import httpx

# 向量字段名：dense 与 sparse 各一路，混合检索时按名字指认
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"

DEFAULT_COLLECTION = "meeting_kb"
# 单次请求的候选数量与超时（检索要快，宁可分页也不要长挂）
PREFETCH_LIMIT = 24
DEFAULT_TIMEOUT = 20.0


class QdrantError(RuntimeError):
    """与向量库交互失败。调用方按"可重试"处理（Temporal activity 会重试）。"""


def base_url() -> str:
    return os.getenv("QDRANT_URL", "http://qdrant:6333").rstrip("/")


def collection_name() -> str:
    return os.getenv("QDRANT_COLLECTION", DEFAULT_COLLECTION)


def _client(timeout: float = DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url(), timeout=timeout)


async def _call(method: str, path: str, *, json: Any = None, timeout: float = DEFAULT_TIMEOUT) -> Any:
    try:
        async with _client(timeout) as client:
            response = await client.request(method, path, json=json)
    except httpx.HTTPError as error:
        raise QdrantError(f"向量库不可达：{type(error).__name__}: {error}") from error
    if response.status_code >= 400:
        raise QdrantError(f"向量库返回 {response.status_code}：{response.text[:300]}")
    try:
        return response.json()
    except ValueError as error:
        raise QdrantError(f"向量库返回的不是 JSON：{response.text[:200]}") from error


def _result(payload: Any) -> Any:
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise QdrantError(f"向量库返回异常结构：{str(payload)[:200]}")
    return payload.get("result")


async def health() -> bool:
    """健康检查：只探连通性，不抛异常（供 /health 汇总用）。"""
    try:
        async with _client(timeout=3.0) as client:
            response = await client.get("/healthz")
        return response.status_code == 200
    except httpx.HTTPError:
        return False


async def ensure_collection(dim: int, *, name: str | None = None) -> None:
    """确保 collection 存在且维度一致。已存在则校验维度，不一致直接报错。

    **不自动重建**：维度不一致意味着换了嵌入模型，此时旧向量必须重嵌，
    自动删库会把还在用的数据一起抹掉——这种事必须由人显式决定。
    """
    if dim <= 0:
        raise QdrantError("嵌入维度必须为正数")
    name = name or collection_name()
    existing = _result(await _call("GET", f"/collections/{name}")) if await _exists(name) else None
    if existing is not None:
        vectors = (existing.get("config") or {}).get("params", {}).get("vectors") or {}
        current = (vectors.get(DENSE_VECTOR) or {}).get("size")
        if current and int(current) != int(dim):
            raise QdrantError(
                f"collection {name} 的向量维度是 {current}，当前嵌入模型是 {dim} 维。"
                "换嵌入模型需要重建知识库（不要自动重建：旧向量可能还在用）")
        return
    await _call("PUT", f"/collections/{name}", json={
        "vectors": {DENSE_VECTOR: {"size": int(dim), "distance": "Cosine"}},
        "sparse_vectors": {SPARSE_VECTOR: {}},
    })


async def _exists(name: str) -> bool:
    try:
        await _call("GET", f"/collections/{name}")
        return True
    except QdrantError:
        return False


async def upsert_chunks(points: Sequence[dict[str, Any]], *, name: str | None = None) -> None:
    """写入/覆盖切片向量。

    `points` 每项形如：
        {"id": 123, "dense": [...], "sparse": {"indices": [...], "values": [...]}, "payload": {...}}
    其中 `id` 就是 kb_chunk.id——不做第二套编号，否则两边迟早对不上。
    """
    if not points:
        return
    name = name or collection_name()
    body = {"points": [{
        "id": int(point["id"]),
        "vector": {DENSE_VECTOR: list(point["dense"]), SPARSE_VECTOR: dict(point["sparse"])},
        "payload": dict(point.get("payload") or {}),
    } for point in points]}
    await _call("PUT", f"/collections/{name}/points?wait=true", json=body)


async def delete_points(ids: Sequence[int], *, name: str | None = None) -> None:
    """按 id 删除切片向量（重建文档时清掉上一版的向量）。"""
    if not ids:
        return
    name = name or collection_name()
    await _call("POST", f"/collections/{name}/points/delete?wait=true",
                json={"points": [int(i) for i in ids]})


def _acl_filter(allowed_meeting_ids: Sequence[int] | None) -> dict[str, Any] | None:
    """把"可见会议集合"翻译成 Qdrant 过滤条件。

    None = 不过滤（管理员）；空序列 = 谁都不许看 → 用一个恒假条件把结果清空。
    之所以不用 None 表示空集合：那会把"没有权限"变成"没有限制"，是最危险的默认值。
    """
    if allowed_meeting_ids is None:
        return None
    ids = [int(i) for i in allowed_meeting_ids]
    if not ids:
        return {"must": [{"key": "meeting_id", "match": {"any": [-1]}}]}
    return {"must": [{"key": "meeting_id", "match": {"any": ids}}]}


async def hybrid_search(
    *,
    dense: Sequence[float],
    sparse: dict[str, Any],
    limit: int = 8,
    allowed_meeting_ids: Sequence[int] | None = None,
    authority: Iterable[str] | None = None,
    name: str | None = None,
) -> list[dict[str, Any]]:
    """混合检索：稠密与稀疏各召回一批，由 Qdrant 在服务端做 RRF 融合。

    为什么用 RRF 而不是加权分数：余弦分数有界、稀疏分数无界，直接线性加权必须
    先各自归一化（归一化方式又是个新参数）；RRF 只看排名，天然规避两路分数量纲不同。
    """
    name = name or collection_name()
    prefetch: list[dict[str, Any]] = [
        {"query": list(dense), "using": DENSE_VECTOR, "limit": PREFETCH_LIMIT},
    ]
    if sparse.get("indices"):
        prefetch.append({"query": {"indices": list(sparse["indices"]),
                                   "values": list(sparse["values"])},
                         "using": SPARSE_VECTOR, "limit": PREFETCH_LIMIT})

    must: list[dict[str, Any]] = []
    acl = _acl_filter(allowed_meeting_ids)
    if acl:
        must.extend(acl["must"])
    if authority:
        must.append({"key": "authority", "match": {"any": [str(a) for a in authority]}})

    body: dict[str, Any] = {
        "prefetch": prefetch,
        "query": {"fusion": "rrf"},
        "limit": max(1, int(limit)),
        "with_payload": True,
    }
    if must:
        body["filter"] = {"must": must}

    result = _result(await _call("POST", f"/collections/{name}/points/query", json=body))
    points = (result or {}).get("points") or []
    return [{
        "chunk_id": int(point["id"]),
        "score": float(point.get("score") or 0.0),
        "payload": dict(point.get("payload") or {}),
    } for point in points]
