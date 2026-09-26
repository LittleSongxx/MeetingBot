"""公开集运行器的共享基础设施。

来历：原 `evaluation/run_eval.py`（合成集驱动器，已删除）中被公开集运行器
共用的部分拆到这里——分块估算的唯一实现、六字段契约的独立校验、评测对
产品 HTTP API 的调用与凭据读取。合成集的驱动与评分逻辑不迁移。

`MAX_CHUNK_CHARS` 仍是唯一真源：产品、适配器、评测器必须一致，否则预算
预留与实际执行不符（实测踩过：四处独立实现导致运行到一半报
"Actual product chunking exceeds reserved budget"）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
from argparse import Namespace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

FIELDS = ("summary", "topics", "viewpoints", "decisions", "pending_items", "risks")
# 六字段条目的独立契约声明（评测侧第二只眼，**不 import 产品实现**）：
# 每个条目 = 固定文本字段 + 类型化槽位（owner/deadline/raised_by，SourcedValue 字典）
# + 溯源字段（origin/evidence）。与产品契约的同步由测试里的"真实产物金样"把守——
# 产品改条目结构时金样测试先红，逼出一次显式同步，而不是让结构检查静默全挂
# （2026-10-05 踩过：契约 v2 给条目加了 origin/evidence，本表没跟上，
# 所有新运行的 structure.schema_valid 系统性全 False）。
ITEM_FIELDS = {
    "topics": ("title", "summary", "origin", "evidence"),
    "viewpoints": ("speaker", "viewpoint", "origin", "evidence"),
    "decisions": ("content", "basis", "owner", "deadline", "origin", "evidence"),
    "pending_items": ("content", "owner", "deadline", "origin", "evidence"),
    "risks": ("content", "level", "suggestion", "raised_by", "origin", "evidence"),
}
# 类型化槽位：值是 SourcedValue 字典，五个键一个不能多也不能少
SLOT_FIELDS = ("owner", "deadline", "raised_by")
SLOT_VALUE_KEYS = ("text", "basis", "normalized", "anchor", "evidence")
# origin 的封闭集合（与产品 ORIGINS 声明一致；同步靠金样测试把守）
ITEM_ORIGINS = ("stated", "derived", "model_inferred")

# 旧版（legacy）条目契约：origin/evidence 溯源字段引入之前的形态。
# AliMeeting4MUG 的冻结运行产物与盲态标注流水线都是这个形态——它们是已冻结的
# 证据资产，不能因为产品契约升级就被判成"不合规"（2026-10-05 踩过：只留新表后，
# alimeeting_review 对 legacy 夹具全部报 missing/extra fields）。
ITEM_FIELDS_LEGACY = {
    "topics": ("title", "summary"),
    "viewpoints": ("speaker", "viewpoint"),
    "decisions": ("content", "basis", "owner_suggestion", "deadline_suggestion"),
    "pending_items": ("content", "owner_suggestion", "deadline_suggestion"),
    "risks": ("content", "level", "suggestion"),
}
def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    """在发起下一个付费操作前先落盘预留/结果（原子写）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def normalized_segments(case: dict) -> list[dict]:
    rows = case.get("segments") or [
        {"start_ms": 0, "end_ms": 60000, "speaker": "转写", "text": case["transcript"]}
    ]
    result = []
    for row in rows:
        text = row.get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{case['id']}: each segment needs non-empty text")
        start, end = row.get("start_ms", 0), row.get("end_ms", 0)
        if type(start) is not int or type(end) is not int or start < 0 or end < start:
            raise ValueError(f"{case['id']}: invalid segment timestamps")
        speaker = row.get("speaker") or "未知说话人"
        if not isinstance(speaker, str) or len(speaker) > 50:
            raise ValueError(f"{case['id']}: speaker label must be at most 50 characters")
        result.append({"start_ms": start, "end_ms": end, "speaker": speaker, "text": text})
    return result


def estimated_generation_calls(case: dict) -> int:
    """生成阶段的调用数：单遍长上下文——每场会议恰好一次。

    （历史上的 6000 字分块 + Map-Reduce 合并已于 2026-10-06 移除；
    旧 manifest 里的 estimated_generation_chunks 等字段是冻结的历史元数据。）
    """
    return 1


def _evidence_errors(value: object, path: str) -> list[str]:
    """证据引句必须是 [{quote, speaker}] 形态（逐字可定位校验在上游门控）。"""
    if not isinstance(value, list):
        return [f"{path} must be an array"]
    errors = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict) or set(entry) != {"quote", "speaker"}:
            errors.append(f"{path}[{index}] must be {{quote, speaker}}")
        elif not all(isinstance(entry[key], str) for key in ("quote", "speaker")):
            errors.append(f"{path}[{index}] fields must be strings")
    return errors


def _slot_errors(value: object, path: str) -> list[str]:
    """类型化槽位必须是五键齐全的 SourcedValue 字典。"""
    if not isinstance(value, dict):
        return [f"{path} must be a sourced-value object"]
    if set(value) != set(SLOT_VALUE_KEYS):
        return [f"{path} must have exactly {SLOT_VALUE_KEYS}"]
    errors = []
    for key in ("text", "basis", "normalized", "anchor"):
        if not isinstance(value[key], str):
            errors.append(f"{path}.{key} must be a string")
    errors.extend(_evidence_errors(value["evidence"], f"{path}.evidence"))
    return errors


def _schema_errors_legacy(output: object) -> list[str]:
    """legacy 条目契约的严格校验（AliMeeting4MUG 冻结产物形态）。"""
    if not isinstance(output, dict):
        return ["output must be a JSON object"]
    errors = []
    if set(output) != set(FIELDS):
        errors.append("output must contain exactly the six minutes fields")
    if not isinstance(output.get("summary"), str):
        errors.append("summary must be a string")
    for field, keys in ITEM_FIELDS_LEGACY.items():
        rows = output.get(field)
        if not isinstance(rows, list):
            errors.append(f"{field} must be an array")
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or set(row) != set(keys):
                errors.append(f"{field}[{index}] has missing/extra fields")
                continue
            if any(not isinstance(row[key], str) for key in keys):
                errors.append(f"{field}[{index}] fields must be strings")
            if field == "risks" and row.get("level") not in {"LOW", "MEDIUM", "HIGH"}:
                errors.append(f"risks[{index}].level is invalid")
    return errors


def schema_errors(output: object, *, contract: str = "v2") -> list[str]:
    """对产品六字段输出契约的独立严格校验（评测侧第二只眼）。

    `contract`：`"v2"`（默认）= 含 origin/evidence 溯源字段的当前产品契约；
    `"legacy"` = 溯源字段引入前的旧形态（AliMeeting4MUG 冻结产物专用）。
    """
    if contract == "legacy":
        return _schema_errors_legacy(output)
    if contract != "v2":
        return [f"unknown contract: {contract}"]
    if not isinstance(output, dict):
        return ["output must be a JSON object"]
    errors = []
    if set(output) != set(FIELDS):
        errors.append("output must contain exactly the six minutes fields")
    if not isinstance(output.get("summary"), str):
        errors.append("summary must be a string")
    for field, keys in ITEM_FIELDS.items():
        rows = output.get(field)
        if not isinstance(rows, list):
            errors.append(f"{field} must be an array")
            continue
        for index, row in enumerate(rows):
            path = f"{field}[{index}]"
            if not isinstance(row, dict) or set(row) != set(keys):
                errors.append(f"{path} has missing/extra fields")
                continue
            for key in keys:
                if key in SLOT_FIELDS:
                    errors.extend(_slot_errors(row[key], f"{path}.{key}"))
                elif key == "evidence":
                    errors.extend(_evidence_errors(row[key], f"{path}.evidence"))
                elif key == "origin":
                    if row[key] not in ITEM_ORIGINS:
                        errors.append(f"{path}.origin is invalid")
                elif not isinstance(row[key], str):
                    errors.append(f"{path}.{key} must be a string")
            if field == "risks" and row.get("level") not in {"LOW", "MEDIUM", "HIGH"}:
                errors.append(f"{path}.level is invalid")
    return errors


def candidates(output: object, field: str) -> list[str | dict]:
    """逐字段展开待匹配文本；不把不同条目的事实拼在一起匹配。"""
    if not isinstance(output, dict):
        return []
    selected = FIELDS if field == "*" else (field,)
    result: list[str | dict] = []
    for name in selected:
        value = output.get(name)
        if isinstance(value, str):
            result.append({"path": name, "text": value})
        elif isinstance(value, list):
            for index, item in enumerate(value):
                text = " | ".join(str(v) for v in item.values()) if isinstance(item, dict) else str(item)
                result.append({"path": f"{name}[{index}]", "text": text})
    return result


def credentials(args: Namespace) -> tuple[str, str]:
    selected: dict[str, str] = {}
    if args.env_file:
        project = Path(__file__).resolve().parents[2]
        path = args.env_file.resolve()
        if not path.is_relative_to(project):
            raise ValueError("--env-file must be inside this project")
        # 只读这两个值；绝不导出或打印模型密钥。
        for line in path.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^\s*(?:export\s+)?(APP_ADMIN_USERNAME|APP_ADMIN_PASSWORD)\s*=\s*(.*)$", line)
            if match:
                try:
                    value = match[2].strip()
                    selected[match[1]] = (" ".join(shlex.split(value, comments=True, posix=True))
                                          if value.startswith(("'", '"'))
                                          else re.split(r"\s+#", value, maxsplit=1)[0].rstrip())
                except ValueError:
                    raise ValueError("Malformed quoted admin credential in --env-file") from None
    username = args.username or os.environ.get("APP_ADMIN_USERNAME") or selected.get("APP_ADMIN_USERNAME")
    password = os.environ.get(args.password_env) or os.environ.get("APP_ADMIN_PASSWORD") or selected.get("APP_ADMIN_PASSWORD")
    if not username or not password:
        raise ValueError("live needs admin username/password from environment or --env-file; passwords are not command arguments")
    return username, password


def api_request(base_url: str, path: str, payload: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["token"] = token
    request = Request(base_url.rstrip("/") + path,
                      data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
    try:
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except (HTTPError, URLError) as error:
        raise RuntimeError(f"HTTP request failed for {path}: {type(error).__name__}; no automatic retry") from None
    if str(result.get("code")) != "200":
        raise RuntimeError(f"API rejected {path}: {result.get('msg', 'unknown error')}")
    return result["data"]
