"""行动项：从已确认纪要的待确认事项/决策里提取可跟踪任务（确定性映射，不调用模型）。"""

from __future__ import annotations

import logging
import re
from datetime import date

from models import ActionItem, MeetingMinutes, User
from tortoise.exceptions import IntegrityError

logger = logging.getLogger(__name__)

_DUE_RE = re.compile(r"(\d{4})[-/年.](\d{1,2})[-/月.](\d{1,2})")
_NAME_SPLIT_RE = re.compile(r"[、,，/和及]")
# 单条纪要最多提取多少条行动项，避免异常数据生成过多
MAX_ITEMS_PER_MINUTES = 50


def parse_due_date(text: str | None) -> date | None:
    """从“2026-09-10 / 2026年9月10日”这类文本解析日期，解析不了返回 None。"""
    if not text:
        return None
    match = _DUE_RE.search(str(text))
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def split_owner_names(text: str | None) -> list[str]:
    """把“张涛、李悦”拆成姓名列表；带括号说明的取括号前姓名。"""
    if not text:
        return []
    raw = str(text).strip()
    names = []
    for part in _NAME_SPLIT_RE.split(raw):
        name = part.strip().split("（")[0].split("(")[0].strip()
        if name and name not in names:
            names.append(name)
    return names


async def build_owner_index() -> dict[str, int]:
    """正常账号的姓名 → 用户ID；同名时保留 None 语义（调用方只在唯一命中时绑定）。"""
    index: dict[str, int | None] = {}
    for user in await User.filter(status="NORMAL"):
        name = (user.name or "").strip()
        if not name:
            continue
        if name in index:
            index[name] = None
        else:
            index[name] = user.id
    return {name: user_id for name, user_id in index.items() if user_id is not None}


def _match_owner(text: str | None, owner_index: dict[str, int]) -> int | None:
    names = split_owner_names(text)
    # 只有“恰好一个姓名且该姓名唯一对应一个账号”时才绑定用户ID，多姓名/重名保留文本
    if len(names) != 1:
        return None
    return owner_index.get(names[0])


def _collect_specs(minutes: MeetingMinutes) -> list[dict]:
    specs = []
    seen_content: set[str] = set()

    def add_spec(spec: dict) -> None:
        from services.evidence_service import normalize

        key = normalize(spec["content"])
        if not key or key in seen_content:
            return
        seen_content.add(key)
        specs.append(spec)
    for index, item in enumerate(minutes.pending_items_json or []):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        add_spec(
            {
                "source_field": "pending_items",
                "source_index": index,
                "content": content,
                "owner_text": item.get("owner_suggestion"),
                "due_text": item.get("deadline_suggestion"),
            }
        )
    for index, item in enumerate(minutes.decisions_json or []):
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        owner_text = item.get("owner_suggestion")
        due_text = item.get("deadline_suggestion")
        # 决策不一定都是任务：只有带负责人/期限的决策才转成行动项
        if not content or not (owner_text or due_text):
            continue
        add_spec(
            {
                "source_field": "decisions",
                "source_index": index,
                "content": content,
                "owner_text": owner_text,
                "due_text": due_text,
            }
        )
    return specs[:MAX_ITEMS_PER_MINUTES]


async def generate_from_minutes(minutes: MeetingMinutes, created_by: int) -> dict:
    """把已确认纪要转成 DRAFT 行动项（幂等：已存在的来源条目跳过）。"""
    owner_index = await build_owner_index()
    created = 0
    skipped = 0
    for spec in _collect_specs(minutes):
        exists = await ActionItem.filter(
            minutes_id=minutes.id,
            source_field=spec["source_field"],
            source_index=spec["source_index"],
        ).exists()
        if exists:
            skipped += 1
            continue
        due_text = str(spec["due_text"]).strip() if spec["due_text"] else None
        try:
            await ActionItem.create(
                meeting_id=minutes.meeting_id,
                minutes_id=minutes.id,
                content=spec["content"][:1000],
                owner_user_id=_match_owner(spec["owner_text"], owner_index),
                owner_name=(str(spec["owner_text"]).strip()[:100] if spec["owner_text"] else None),
                due_date=parse_due_date(due_text),
                due_text=(due_text[:100] if due_text else None),
                status="DRAFT",
                source_field=spec["source_field"],
                source_index=spec["source_index"],
                created_by=created_by,
            )
            created += 1
        except IntegrityError:
            # 并发点两次生成时唯一索引兜底，后一个请求按“已存在”跳过
            skipped += 1
    logger.info(
        "行动项提取完成",
        extra={"minutes_id": minutes.id, "created_count": created, "skipped_count": skipped},
    )
    return {"created": created, "skipped": skipped}
