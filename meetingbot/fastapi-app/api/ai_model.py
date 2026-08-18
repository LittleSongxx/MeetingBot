from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from common.auth import require_admin
from common.exception_handler import CustomException
from common.result import PageInfo, Result
from common.times import format_datetime
from models import AgentRun, AiModelConfig, MeetingMinutes, TranscriptionTask, User

router = APIRouter(prefix="/aiModel")


class AiModelPayload(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    # 编辑时回填，新增时不存在
    id: int | None = None
    # 弹窗配置名称输入框
    name: str | None = None
    # 弹窗模型用途下拉框，不传时按转写处理
    model_type: str = "TRANSCRIPTION"
    # 弹窗服务协议只读输入框
    provider: str = "OPENAI_COMPATIBLE"
    # 弹窗 API 地址输入框
    base_url: str | None = None
    # 弹窗 API Key 输入框，编辑时可能是掩码字符串
    api_key: str | None = None
    # 弹窗模型名称输入框
    model_name: str | None = None
    # 弹窗超时秒数数字框
    timeout_seconds: int = 600
    # 弹窗启用开关，不传时按不启用处理
    enabled: bool = False


def mask_api_key(api_key: str) -> str:
    # 还没填过 Key 时返回空串，页面上这一格是空的
    if not api_key:
        return ""
    # 太短的 Key 全部打码，避免露出有效片段
    if len(api_key) <= 8:
        return "********"
    # 常规 Key 保留前 3 位和后 4 位，中间用 **** 代替，便于管理员辨认是哪一把 Key
    return api_key[:3] + "****" + api_key[-4:]


def model_dict(config: AiModelConfig) -> dict:
    return {
        # 编辑和删除都用这个ID
        "id": config.id,
        # 列表“配置名称”列
        "name": config.name,
        # 列表“模型用途”列，页面用 modelTypeText 翻译成中文
        "model_type": config.model_type,
        # 列表“服务协议”列
        "provider": config.provider,
        # 列表“API地址”列
        "base_url": config.base_url,
        # 列表“API Key”列，只给掩码，完整 Key 不出后端
        "api_key": mask_api_key(config.api_key),
        # 列表“模型名称”列
        "model_name": config.model_name,
        # 列表“超时秒数”列
        "timeout_seconds": config.timeout_seconds,
        # 列表“状态”列，页面按它显示“已启用”或“未启用”
        "enabled": config.enabled,
        "create_time": format_datetime(config.create_time),
        "update_time": format_datetime(config.update_time),
    }


async def validate_payload(payload: AiModelPayload, old_config: AiModelConfig | None = None):
    # 三个必填项，页面校验之外后端独立再校验一次
    if not payload.name or not payload.base_url or not payload.model_name:
        raise CustomException("配置名称、API地址和模型名称不能为空")
    # 用途只接受下拉框里的四个取值
    if payload.model_type not in {"TRANSCRIPTION", "MINUTES", "AGENT", "SPEAKER"}:
        raise CustomException("模型类型参数错误")
    # 纪要、纪要自检和说话人匹配走 OpenAI 兼容协议；转写走百炼自己的录音文件识别协议
    if payload.provider not in {"OPENAI_COMPATIBLE", "DASHSCOPE"}:
        raise CustomException("模型服务商参数错误")
    # 超时范围和页面数字框的 min、max 保持一致
    if payload.timeout_seconds < 30 or payload.timeout_seconds > 3600:
        raise CustomException("请求超时时间必须在30至3600秒之间")
    # 编辑时如果这次没提交新 Key，用数据库里的旧 Key 参与判断
    api_key = payload.api_key or (old_config.api_key if old_config else "")
    # 没有 Key 的配置一旦被启用，后面各章调用模型时必然失败，所以在这里就拦住
    if payload.enabled and not api_key:
        raise CustomException("启用模型前请填写API Key")


@router.post("/add")
async def add(payload: AiModelPayload, _: User = Depends(require_admin)):
    await validate_payload(payload)
    # 本次要启用时，先把同用途下已启用的配置全部停用，保证一个用途只有一条生效
    if payload.enabled:
        await AiModelConfig.filter(model_type=payload.model_type, enabled=True).update(enabled=False)
    await AiModelConfig.create(
        name=payload.name,
        model_type=payload.model_type,
        provider=payload.provider,
        # 去掉地址末尾的斜杠，转写服务在 base_url 后面拼接接口路径时不会出现双斜杠
        base_url=payload.base_url.rstrip("/"),
        # 没填 Key 时存空串而不是 None，方便后面统一按字符串处理
        api_key=payload.api_key or "",
        model_name=payload.model_name,
        timeout_seconds=payload.timeout_seconds,
        enabled=payload.enabled,
    )
    return Result.success()


@router.put("/update")
async def update(payload: AiModelPayload, _: User = Depends(require_admin)):
    # 编辑必须带上配置ID
    if payload.id is None:
        raise CustomException("模型配置ID不能为空")
    config = await AiModelConfig.get_or_none(id=payload.id)
    if config is None:
        raise CustomException("模型配置不存在")
    # 只有真的要改用途时才做下面这串检查
    if payload.model_type != config.model_type:
        # 逐张业务表看这条配置有没有被用过
        # 第 8 章的转写任务
        used = await TranscriptionTask.filter(model_config_id=config.id).exists()
        # 第 11 章的会议纪要
        used = used or await MeetingMinutes.filter(model_config_id=config.id).exists()
        # 第 13 章的纪要自检运行
        used = used or await AgentRun.filter(model_config_id=config.id).exists()
        # 用过就锁死用途，让历史记录里的 model_config_id 始终指向含义一致的配置
        if used:
            raise CustomException("已被业务数据使用的模型不能修改用途")
    new_api_key = payload.api_key
    # 页面回显的是掩码，用户没改动时提交上来的就带着 ****，这种情况沿用数据库里的原 Key
    # 输入框被清空时同样沿用原 Key
    if not new_api_key or "****" in new_api_key:
        new_api_key = config.api_key
    # 把还原后的 Key 写回 payload，让下面的启用校验拿到真实值
    payload.api_key = new_api_key
    await validate_payload(payload, config)
    # 本次要启用时，把同用途下其他已启用的配置停用，自己不在停用范围内
    if payload.enabled:
        await AiModelConfig.filter(model_type=payload.model_type, enabled=True).exclude(
            id=config.id
        ).update(enabled=False)
    await AiModelConfig.filter(id=config.id).update(
        name=payload.name,
        model_type=payload.model_type,
        provider=payload.provider,
        base_url=payload.base_url.rstrip("/"),
        # 写回的是还原后的真实 Key，不会把掩码字符串存进数据库
        api_key=new_api_key,
        model_name=payload.model_name,
        timeout_seconds=payload.timeout_seconds,
        enabled=payload.enabled,
    )
    return Result.success()


@router.delete("/delete/{config_id}")
async def delete(config_id: int, _: User = Depends(require_admin)):
    config = await AiModelConfig.get_or_none(id=config_id)
    if config is None:
        raise CustomException("模型配置不存在")
    # 逐类业务数据分开检查，好让提示能说清是被哪个功能占用
    # 第 8 章的转写任务记着 model_config_id
    if await TranscriptionTask.filter(model_config_id=config_id).exists():
        raise CustomException("该模型配置已被转写任务使用，不能删除")
    # 第 11 章的会议纪要记着生成时用的模型
    if await MeetingMinutes.filter(model_config_id=config_id).exists():
        raise CustomException("该模型配置已被会议纪要使用，不能删除")
    # 第 13 章的纪要自检运行记着自检模型
    if await AgentRun.filter(model_config_id=config_id).exists():
        raise CustomException("该模型配置已被Agent运行使用，不能删除")
    await config.delete()
    return Result.success()


@router.get("/selectPage")
async def select_page(
    # name 来自页面配置名称输入框
    name: str = "",
    # model_type 来自页面模型用途下拉框
    model_type: str = "",
    page_num: int = 1,
    page_size: int = 10,
    # 模型配置只对管理员开放
    _: User = Depends(require_admin),
):
    # 配置名称模糊匹配，空串时相当于不加条件
    query = AiModelConfig.filter(name__contains=name)
    # 用途下拉框有值时追加等值条件
    if model_type:
        query = query.filter(model_type=model_type)
    # 已启用的排在最前，同样状态下按ID倒序，管理员一眼能看到当前生效的配置
    configs = await query.order_by("-enabled", "-id").offset(
        (page_num - 1) * page_size
    ).limit(page_size)
    total = await query.count()
    return Result.success(
        PageInfo(total=total, list=[model_dict(config) for config in configs])
    )
