"""统一的可见性口径：谁能看哪场会。

**为什么单独成模块**：这条规则（自己创建 ｜ 自己主持 ｜ 自己参会）原本在五处各写一遍——
会议下拉框、会议列表、纪要列表、转写列表，以及新增的知识库检索。
五份实现意味着五处漂移风险，而"可见性"漂移的后果是越权，不是显示不对。

**两条不同形状的接口，别混用**：

- `visible_meeting_ids(user)` → 一个 `id` 列表，适合"把这个条件塞进另一个查询"
  （例如 `query.filter(meeting_id__in=...)`），也适合向量库的 payload 过滤
- `has_meeting_access(meeting, user)` → 布尔判断，适合"已经拿到了具体的会议行"

两者都由 `ADMIN` 全通过。原先 `has_meeting_access` 住在 `api/meeting.py` 里，
但 services 层也要用它（知识库的工具层），而 services 反向依赖 api 是错的层次，
所以上移到 common。
"""

from __future__ import annotations

from models import Meeting, MeetingParticipant, User
from tortoise.expressions import Q


async def participant_meeting_ids(user: User) -> list[int]:
    """用户以参会人身份参与的会议 id。单独开出来是因为调用方常要复用它。"""
    return [int(i) for i in await MeetingParticipant.filter(
        user_id=user.id).values_list("meeting_id", flat=True)]


def visible_meeting_query(user: User, participant_ids: list[int]):
    """返回"可见会议"的 `Meeting` 查询条件（未执行）。

    用一个不存在的 id 兜底空集合：`id__in=[]` 在部分后端上等价于"不过滤"，
    那会把"没有权限"变成"没有限制"。
    """
    return Q(creator_id=user.id) | Q(host_id=user.id) | Q(id__in=participant_ids or [-1])


async def visible_meeting_ids(user: User, *, requested: list[int] | None = None) -> list[int] | None:
    """当前用户可见的会议 id。

    - 管理员且未指定范围 → 返回 `None`，含义是"不做过滤"（不是"没有会议"）
    - 指定了 `requested` → 取交集（先确认这些会议存在且可见）
    - 其他身份 → 返回可见 id 列表，可能为空列表

    `None` 与空列表是两种语义，调用方（尤其向量检索的过滤）必须区分对待。
    """
    if user.role == "ADMIN" and not requested:
        return None

    query = Meeting.all()
    if user.role != "ADMIN":
        query = query.filter(visible_meeting_query(user, await participant_meeting_ids(user)))
    if requested:
        query = query.filter(id__in=requested)
    return [int(i) for i in await query.values_list("id", flat=True)]


async def has_meeting_access(meeting: Meeting, user: User) -> bool:
    """能否访问某一场会议：管理员全通过，否则看创建人／主持人／参会人。"""
    if user.role == "ADMIN":
        return True
    if meeting.creator_id == user.id or meeting.host_id == user.id:
        return True
    return await MeetingParticipant.exists(meeting_id=meeting.id, user_id=user.id)
