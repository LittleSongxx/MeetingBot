"""评测侧调用模型的唯一入口。

抽出来的理由与 `common/textsim.py` 相同：**同一件事只能有一份实现**。
AI 裁决（`ai_adjudicate.py`）与完整性清单（`completeness.py`）都要调模型，
各写一份 `urllib` 请求必然漂移——一处改了超时、另一处没改，或者一处的
`response_format` 用错，真实运行时才失败（本项目在 `response_format` 上踩过一次）。

因此这里只有两个函数：读端点配置、发一次请求。**key 只从环境读，绝不落盘、绝不打印。**
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
import urllib.request
from typing import Any, Iterator, Mapping


class ModelClientError(RuntimeError):
    """调用失败。消息里**不含** API key。"""


# ---------------------------------------------------------------------------
# 传输层：本机 IPv6 路由黑洞化会让每次调用先白等 ~133 秒
#
# 实测（2026-09-26，宿主）：`socket.create_connection(("api.deepseek.com", 443))`
# 耗时 **133.5 秒**才返回，而容器内同一行是 **0.0 秒**。原因不是供应商慢：
# 宿主 `getaddrinfo` 先返回 AAAA（`2001:2::6d`，不可达），Python 按顺序逐个尝试，
# 于是每次调用先耗尽 IPv6 的 SYN 重传（`tcp_syn_retries=6`）再回落到 IPv4
# （`198.18.0.114`，本地透明代理的 fake-IP，连接 0.00 秒）。
#
# 这解释了此前被记成「接口突然变慢」的一整组观测：同一探针在两个供应商上
# 分别耗时 136.1 / 135.4 秒，而一次真实调用的生成内容只有几十 token——
# 时间根本不花在模型上。**据此得出的「开关思考对耗时没有影响」也因此不可读**：
# 该对照被一个恒定的传输停顿主导，它测不出任何模型侧差异。
#
# 默认只走 IPv4；`MODEL_CLIENT_IP_FAMILY=any` 可恢复系统默认（用于复核本机网络）。
IP_FAMILY_ENV = "MODEL_CLIENT_IP_FAMILY"


def _address_families() -> tuple[int, ...]:
    """返回允许的地址族（按顺序）。默认只允许 IPv4，见上方实测。"""
    choice = (os.getenv(IP_FAMILY_ENV) or "prefer_ipv4").strip().lower()
    if choice == "any":
        return ()
    if choice in ("ipv4", "prefer_ipv4", "4"):
        return (socket.AF_INET,)
    if choice in ("ipv6", "6"):
        return (socket.AF_INET6,)
    return (socket.AF_INET,)


@contextlib.contextmanager
def _restrict_address_family() -> Iterator[None]:
    """在调用期间限制 `getaddrinfo` 的返回族。

    `urllib` 自己会解析域名，因此只能在这一层收窄——包一层再还原，
    不改变任何请求语义，只改变候选地址的顺序/集合。
    """
    allowed = _address_families()
    if not allowed:
        yield
        return
    original = socket.getaddrinfo

    def patched(host, port, family=0, type=0, proto=0, flags=0):  # type: ignore[no-untyped-def]
        infos = original(host, port, family, type, proto, flags)
        kept = [info for info in infos if info[0] in allowed]
        return kept or infos

    socket.getaddrinfo = patched  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.getaddrinfo = original  # type: ignore[assignment]


def probe_transport(host: str, port: int = 443, *, timeout: float = 3.0) -> dict[str, Any]:
    """逐个地址族测一次连接，返回**可落盘的**诊断。

    这是「先让失败可观测」那一步：以前只能看到「这次调用 135 秒」，
    分不清是连接卡住还是模型在生成；现在连接成本单独可测、可记录。
    """
    report: dict[str, Any] = {"host": host, "port": port, "attempts": [],
                              "ip_family_env": os.getenv(IP_FAMILY_ENV) or "prefer_ipv4"}
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as error:
        report["error"] = f"{type(error).__name__}: {error}"
        return report
    seen: set[tuple[str, str]] = set()
    for family, _, _, _, address in infos:
        key = (family.name, address[0])
        if key in seen:
            continue
        seen.add(key)
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        started = time.monotonic()
        try:
            sock.connect(address)
            report["attempts"].append({"family": family.name, "address": address[0],
                                       "ok": True,
                                       "seconds": round(time.monotonic() - started, 3)})
        except OSError as error:
            report["attempts"].append({"family": family.name, "address": address[0],
                                       "ok": False,
                                       "seconds": round(time.monotonic() - started, 3),
                                       "error": type(error).__name__})
        finally:
            sock.close()
    blocked = [a["family"] for a in report["attempts"] if not a["ok"]]
    if blocked:
        report["diagnosis"] = (
            f"地址族 {sorted(set(blocked))} 不可达；若它排在可用族之前，"
            f"每次调用都会先耗尽它的连接重传才回落——这不是供应商慢。"
        )
    return report


# 第二个模型族：跨族裁决必须有**不同族**的第二个标注者，同族只能测复现性。
# 主机不同 → 推理参数形态不同，因此这里连形态由谁决定也一并声明（读产品的声明表）。
PROVIDERS: dict[str, dict[str, str]] = {
    "default": {"base_env": "LLM_BASE_URL", "model_env": "LLM_MODEL",
                "key_env": "LLM_API_KEY", "fallback_key_env": "DASHSCOPE_API_KEY",
                "base_default": "https://api.deepseek.com", "model_default": "deepseek-flash"},
    "qwen": {"base_env": "QWEN_BASE_URL", "model_env": "QWEN_MODEL",
             "key_env": "DASHSCOPE_API_KEY", "fallback_key_env": "",
             "base_default": "https://dashscope.aliyuncs.com/compatible-mode/v1",
             "model_default": "qwen3.8-max-0902"},
    # 2026-09-26 移除 glm / kimi 两族：glm 持续 429（硬配额），kimi key 401 无效，
    # 且用户决定不再使用。跨族独立判定当前只剩 qwen 一族；历史产物中的
    # label_b / glm / kimi 字段不受影响（标签只追加不覆盖的纪律不变）。
    # 版本段 URL 拼接逻辑（/v4 类基地址）保留在 endpoint()，由注入假 spec 的测试锁住。

}


def endpoint(provider: str = "default") -> tuple[str, str, str]:
    """（chat completions URL，模型名，API key）。按 `provider` 选族，默认主模型。

    基地址容错：可能带也可能不带 `/v1`，两种都要拼对。
    """
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ModelClientError(f"未知的 provider：{provider!r}（可选 {sorted(PROVIDERS)}）")
    base = (os.getenv(spec["base_env"]) or spec["base_default"]).rstrip("/")
    model = os.getenv(spec["model_env"]) or spec["model_default"]
    key = (os.getenv(spec["key_env"]) or "").strip()
    if not key and spec["fallback_key_env"]:
        key = (os.getenv(spec["fallback_key_env"]) or "").strip()
    if not key:
        raise ModelClientError(
            f"provider={provider} 没有可用凭据（{spec['key_env']} 为空）"
        )
    # 版本段泛化：以 /v<数字> 结尾的基地址（/v1、/v4、…）都视为版本根，直接拼
    # chat/completions；否则补 /v1。原来只认 /v1，智谱的 /api/paas/v4 会被错拼成
    # /v4/v1/chat/completions——按"版本段"这个**类别**处理，而不是逐家写分支。
    import re as _re
    if _re.search(r"/v\d+$", base):
        url = f"{base}/chat/completions"
    else:
        url = f"{base}/v1/chat/completions"
    return url, model, key


def _thinking_extra_body(base_url: str, enabled: bool) -> dict[str, Any] | None:
    """关/开思考的参数形态。**读产品的声明表**，不在评测侧另写一份。

    产品 `common/providers.thinking_extra_body` 按主机决定形态（DeepSeek 用
    `{"thinking": {...}}`，DashScope 用 `enable_thinking`）。评测侧复用它，
    否则换供应商时两处会漂移。产品目录不可用时退回 DeepSeek 形态并如实说明。
    """
    import sys as _sys
    from pathlib import Path as _Path

    product_root = _Path(__file__).resolve().parents[2] / "ai_meeting" / "fastapi-app"
    if (product_root / "common" / "providers.py").exists():
        if str(product_root) not in _sys.path:
            _sys.path.insert(0, str(product_root))
        try:
            from common import providers as _providers  # type: ignore
            shape = _providers.thinking_shape(base_url)
            return _providers.thinking_extra_body(shape, enabled)
        except Exception:
            pass
    return {"thinking": {"type": "enabled" if enabled else "disabled"}}


def call_json(url: str, model: str, key: str, system: str, user: str,
              *, timeout: int = 180, max_tokens: int | None = None,
              total_deadline: int | None = None,
              thinking: bool | None = None) -> dict[str, Any]:
    """发一次对话请求并解析返回的 JSON 对象。

    `response_format=json_object` 是当前供应商支持的形态（严格 schema 不支持，
    见 `common/providers.py` 的能力声明）；解析失败会抛 `ModelClientError`，
    由调用方记进产物而不是静默丢一条。

    **`total_deadline` 不是可选项**：`timeout` 只是 socket 层每次操作的超时，
    服务端若缓慢地滴流字节，socket 会一直有活动，于是整个调用可以无限期挂着——
    实测踩到：一次完整性判定的调用挂了 12 分钟以上，进程停在 `do_sys_poll`，
    没有任何输出。这里用**总墙钟上限**兜住它，超时抛 `ModelClientError` 并由调用方记录。
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if thinking is not None:
        # 推理 token 计入输出且很贵：机械类调用关掉（产品也这么做，见 providers 的策略表）。
        # **必须并到 body 根层**：产品用的是 ChatOpenAI(extra_body=...)，SDK 会把
        # extra_body 的内容合并进请求体根层；这里直接发 HTTP，若照抄 `extra_body` 这个键名，
        # 供应商会忽略它——实测：开关"生效"后生成调用仍耗时 159 秒，与开思考时一样，
        # 即**开关静默无效**。合并到根层后同一调用应在十秒量级。
        base = url.split("/v1/")[0]
        payload.update(_thinking_extra_body(base, thinking) or {})
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    deadline = time.monotonic() + float(total_deadline or timeout * 2)
    # 两段分开计时。**名字必须准确**：第一段到"拿到响应头"为止，它包含 TCP 连接、
    # TLS 握手**以及服务端思考到吐第一个字节的时间**，因此它大不等于网络卡
    # ——本机就踩过：同一字段把"连接 0.1 秒 + 服务端沉默 317 秒"记成了"连接 317 秒"。
    # 要判网络单独用 `probe_transport()`；这一段大只能说明"服务端迟迟不给响应头"。
    timings: dict[str, Any] = {"to_headers_ms": None, "body_read_ms": None}
    started = time.monotonic()
    try:
        with _restrict_address_family(), urllib.request.urlopen(request, timeout=timeout) as response:
            timings["to_headers_ms"] = int((time.monotonic() - started) * 1000)
            reader = response.read
            chunks: list[bytes] = []
            while True:
                chunk = reader(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"总时长超过 {int(total_deadline or timeout * 2)} 秒（服务端持续滴流但没有结束）"
                    )
            body = json.loads(b"".join(chunks).decode("utf-8"))
            timings["body_read_ms"] = (int((time.monotonic() - started) * 1000)
                                       - (timings["to_headers_ms"] or 0))
    except Exception as error:  # urllib 的异常层次在不同版本下不一致，统一成自己的类型
        message = str(error)
        if key and key in message:  # 双保险：任何情况下都不把 key 带进异常文本
            message = message.replace(key, "<KEY>")
        # 失败时也把"等到响应头用了多久"带出来：那是模型侧还是网络侧，靠它分辨
        if timings["to_headers_ms"] is None:
            timings["to_headers_ms"] = int((time.monotonic() - started) * 1000)
            message = (f"{message}（等待响应头已耗时 {timings['to_headers_ms']} ms；"
                       "这一段时间包含连接与建立，也包含服务端思考到发出响应头，"
                       "要区分网络与服务端请用 probe_transport）")
        raise ModelClientError(f"{type(error).__name__}: {message[:300]}") from error
    try:
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, ValueError) as error:
        raise ModelClientError(f"返回不是可解析的 JSON 对象：{str(error)[:200]}") from error
    if not isinstance(parsed, Mapping):
        raise ModelClientError(f"返回的 JSON 不是对象：{type(parsed).__name__}")
    return {"parsed": dict(parsed), "usage": body.get("usage") or {}, "timings": timings}
