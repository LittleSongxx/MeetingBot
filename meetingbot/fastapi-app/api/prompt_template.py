from fastapi import APIRouter, Depends
from pydantic import BaseModel

from common.auth import require_admin
from common.exception_handler import CustomException
from common.result import PageInfo, Result
from common.times import format_datetime
from models import PromptTemplate, User

router = APIRouter(prefix="/promptTemplate")


class PromptPayload(BaseModel):
    # 编辑必须带 id，后端按它定位要更新的那条记录
    id: int | None = None
    # 页面原样带回来的编码，后端按它查必需变量表
    code: str | None = None
    # 弹窗模板名称输入框
    name: str | None = None
    # 弹窗里只读展示，原样带回来
    scene_type: str = "MEETING_MINUTES"
    # 弹窗系统指令文本域
    system_prompt: str | None = None
    # 弹窗用户模板文本域
    user_prompt: str | None = None
    # 弹窗启用开关
    enabled: bool = True


def prompt_dict(prompt: PromptTemplate) -> dict:
    return {
        "id": prompt.id,
        # code 也回给前端，编辑保存时要原样带回去做校验
        "code": prompt.code,
        # 表格“模板名称”列，也回填弹窗的模板名称输入框
        "name": prompt.name,
        # 表格“应用场景”列，页面用 sceneText 翻成中文
        "scene_type": prompt.scene_type,
        # 编辑弹窗“系统指令”文本域的初始值
        "system_prompt": prompt.system_prompt,
        # 编辑弹窗“用户模板”文本域的初始值
        "user_prompt": prompt.user_prompt,
        # 表格“状态”列和弹窗“启用模板”开关
        "enabled": prompt.enabled,
        # 时间统一格式化成 yyyy-MM-dd HH:mm:ss 再给前端
        "create_time": format_datetime(prompt.create_time),
        "update_time": format_datetime(prompt.update_time),
    }


async def validate_prompt(payload: PromptPayload):
    # 四项文本直接对应编辑弹窗里的字段，任一为空都无法组成模型请求
    if not payload.code or not payload.name or not payload.system_prompt or not payload.user_prompt:
        raise CustomException("Prompt编码、名称、系统指令和用户模板不能为空")
    # 五条模板都是系统预置的，页面上只能改内容不能改编码，这里挡住编码被换成别的值
    if payload.code not in {"MEETING_MINUTES", "MINUTES_REVIEW", "MINUTES_REFINE", "SPEAKER_MATCH"}:
        raise CustomException("Prompt编码参数错误")
    required_variables = {
        "MEETING_MINUTES": {"{meeting_title}", "{transcript}"},
        # 审查要同时看纪要和原文，所以这两个变量缺一不可
        "MINUTES_REVIEW": {"{meeting_title}", "{minutes_json}", "{transcript}"},
        # 重写还需核对原文，不能只按审核意见补写负责人或期限。
        "MINUTES_REFINE": {"{meeting_title}", "{minutes_json}", "{issues}", "{transcript}"},
        "SPEAKER_MATCH": {"{meeting_title}", "{participants}", "{transcript}"},
    }[payload.code]
    # 变量被管理员误删时，渲染出来的提示词就缺了关键材料，这里直接拒绝保存
    missing = [variable for variable in required_variables if variable not in payload.user_prompt]
    if missing:
        raise CustomException("用户模板缺少必需变量：" + "、".join(missing))
    # 编码唯一，编辑时把自己排除在查重范围外
    query = PromptTemplate.filter(code=payload.code)
    if payload.id:
        query = query.exclude(id=payload.id)
    if await query.exists():
        raise CustomException("Prompt编码重复")


@router.put("/update")
async def update(payload: PromptPayload, _: User = Depends(require_admin)):
    # 只做更新，不存在的 id 直接拒绝，页面上也没有新增入口
    if payload.id is None or not await PromptTemplate.filter(id=payload.id).exists():
        raise CustomException("Prompt模板不存在")
    # 非空、编码合法、必需变量齐全、编码不重复，任一不满足都抛出提示
    await validate_prompt(payload)
    # 除 id 外的字段整体写回，update_time 由数据库自动刷新
    await PromptTemplate.filter(id=payload.id).update(**payload.model_dump(exclude={"id"}))
    return Result.success()


@router.get("/selectPage")
async def select_page(
    name: str = "", sceneType: str = "", pageNum: int = 1, pageSize: int = 10, _: User = Depends(require_admin)
):
    # require_admin 拦住员工，这个页面只有管理员能看
    # name 来自模板名称输入框，空串时相当于不加条件
    query = PromptTemplate.filter(name__contains=name)
    # sceneType 来自应用场景下拉框，有值时追加等值条件
    if sceneType:
        query = query.filter(scene_type=sceneType)
    # 按主键正序，页面上的顺序就是建库脚本插入的顺序：先纪要两条，再自检两条，最后说话人匹配
    rows = await query.order_by("id").offset((pageNum - 1) * pageSize).limit(pageSize)
    return Result.success(PageInfo(total=await query.count(), list=[prompt_dict(row) for row in rows]))
