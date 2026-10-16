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

import logging
import os
from typing import Any, Iterable, Sequence

import httpx

logger = logging.getLogger("meetingbot.qdrant")

# 向量字段名：dense 与 sparse 各一路，混合检索时按名字指认
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"

DEFAULT_COLLECTION = "meeting_kb"
# 单次请求的候选数量与超时（检索要快，宁可分页也不要长挂）
PREFETCH_LIMIT = 24
DEFAULT_TIMEOUT = 20.0

# 会议开始时间在 payload 里的键名（epoch 秒）。过滤"最近七天""上周那场会"用的就是它——
# 注意它与切片里的 `start_ms` **不是同一条时间轴**：`start_ms` 是录音内部的偏移量
# （"第 51 秒说了什么"），而这个键回答"这场会是什么时候开的"。
MEETING_START_KEY = "meeting_start_epoch"

# 会被过滤的字段 + 索引类型。**官方文档明确要求"给每一个会被过滤的字段建 payload 索引"**
# （Qdrant《Indexing》）：没有索引时查询规划器只能把 payload 读出来逐条比对，
# "会不必要地消耗集群资源、拖慢其他查询"；而且要**在建库时就建**——过滤感知的 HNSW 边
# 只在建索引之后写入，后补索引拿不到那部分图结构（后补只省了扫描，没省图）。
#
# 这里的取值全部是"硬条件"（权限、权威级、切片类型、时间范围、来源录音），
# 与官方文档的判断口径一致：**硬条件进过滤，偏好（如"优先最近的"）该进打分而不是过滤**。
INDEXED_FIELDS: tuple[tuple[str, str], ...] = (
    ("meeting_id", "integer"),
    ("authority", "keyword"),
    ("chunk_type", "keyword"),
    (MEETING_START_KEY, "integer"),
    ("source_id", "integer"),
)


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

    **创建是幂等的**：两个索引任务并发跑到这里时，后到的会收到 409
    （collection already exists）。那不是错误——目标状态已经达成；实测就撞过一次
    （两个转写任务同时入队），当时整条索引任务被 409 判失败、切片留在库里没向量。
    所以 PUT 失败后**重新确认一次存在性**，存在即通过（而不是靠匹配错误文本）。
    """
    if dim <= 0:
        raise QdrantError("嵌入维度必须为正数")
    name = name or collection_name()
    existing = _result(await _call("GET", f"/collections/{name}")) if await _exists(name) else None
    if existing is None:
        try:
            await _call("PUT", f"/collections/{name}", json={
                "vectors": {DENSE_VECTOR: {"size": int(dim), "distance": "Cosine"}},
                "sparse_vectors": {SPARSE_VECTOR: {}},
            })
            await ensure_payload_indexes(name)
            return
        except QdrantError:
            # 并发创建：重新读一次，读到了就说明另一个请求建好了
            if not await _exists(name):
                raise
            existing = _result(await _call("GET", f"/collections/{name}"))
    # 已存在的库（老部署、或并发创建的另一方）：把索引补齐。后补索引拿不到
    # 过滤感知的 HNSW 边，但过滤本身仍然从索引受益——这条限制写在 ensure_payload_indexes 里
    await ensure_payload_indexes(name)
    vectors = (existing.get("config") or {}).get("params", {}).get("vectors") or {}
    current = (vectors.get(DENSE_VECTOR) or {}).get("size")
    if current and int(current) != int(dim):
        raise QdrantError(
            f"collection {name} 的向量维度是 {current}，当前嵌入模型是 {dim} 维。"
            "换嵌入模型需要重建知识库（不要自动重建：旧向量可能还在用）")


async def ensure_payload_indexes(name: str | None = None) -> list[str]:
    """确保过滤字段都有 payload 索引，返回**这次真正新建的**字段名。

    幂等：已经存在的索引重复 PUT 不会报错（Qdrant 只在类型冲突时报）。
    索引是过滤器性能的前提，而漏建的表现是"查询变慢"而不是报错——这种漂移最难发现，
    所以把它做成启动/建库路径上的固定一步，而不是靠文档提醒。
    """
    name = name or collection_name()
    created: list[str] = []
    for field, schema in INDEXED_FIELDS:
        try:
            await _call("PUT", f"/collections/{name}/index",
                        json={"field_name": field, "field_schema": schema})
            created.append(field)
        except QdrantError as error:
            # 权限受限的部署可能不允许建索引：**只记日志不打断**——过滤仍然可用（更慢）
            logger.warning("建 payload 索引 %s.%s 失败（过滤仍可用，只是更慢）：%s",
                           name, field, error)
    return created


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


def _match_any(key: str, values: Iterable[Any]) -> dict[str, Any] | None:
    """一组候选值里任一个命中（Qdrant 的 `match.any`）。空集合返回 None（不加条件）。

    用 `match.any` 而不是多个 `should`：官方文档明确说前者更快，值多时差别明显。
    """
    items = [int(v) if isinstance(v, (int, float)) else str(v) for v in values]
    if not items:
        return None
    return {"key": key, "match": {"any": items}}


def _time_range(meeting_time_from: int | None, meeting_time_to: int | None) -> dict[str, Any] | None:
    """按会议开始时间过滤（epoch 秒，闭区间）。两个端点都不给则不加条件。"""
    bounds: dict[str, int] = {}
    if meeting_time_from is not None:
        bounds["gte"] = int(meeting_time_from)
    if meeting_time_to is not None:
        bounds["lte"] = int(meeting_time_to)
    if not bounds:
        return None
    return {"key": MEETING_START_KEY, "range": bounds}


async def hybrid_search(
    *,
    dense: Sequence[float],
    sparse: dict[str, Any],
    limit: int = 8,
    allowed_meeting_ids: Sequence[int] | None = None,
    authority: Iterable[str] | None = None,
    chunk_type: Iterable[str] | None = None,
    source_ids: Iterable[int] | None = None,
    meeting_time_from: int | None = None,
    meeting_time_to: int | None = None,
    name: str | None = None,
) -> list[dict[str, Any]]:
    """混合检索：稠密与稀疏各召回一批，由 Qdrant 在服务端做 RRF 融合。

    为什么用 RRF 而不是加权分数：余弦分数有界、稀疏分数无界，直接线性加权必须
    先各自归一化（归一化方式又是个新参数）；RRF 只看排名，天然规避两路分数量纲不同。

    过滤条件全部落在 `must` 上（与权限过滤同一个位置）：权限是"必须满足"的硬条件，
    业务过滤（权威级/切片类型/会议时间）同样是硬条件——把它们写成 `should` 会让
    不满足条件的点照样被召回，那是最难发现的一类错误（结果看着有，其实不该有）。
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
    if chunk_type:
        must.append({"key": "chunk_type", "match": {"any": [str(t) for t in chunk_type]}})
    if source_ids:
        # 来源录音（转写任务 id）：一场会可能有多段录音，"下午这场会"要靠它定位
        source_filter = _match_any("source_id", source_ids)
        if source_filter:
            must.append(source_filter)
    time_range = _time_range(meeting_time_from, meeting_time_to)
    if time_range:
        must.append(time_range)

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
