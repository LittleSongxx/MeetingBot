from fastapi import APIRouter, Depends

from common.auth import get_current_user
from common.result import Result
from models import User
from services.observability_service import build_observability

router = APIRouter(prefix="/observability")


@router.get("/overview")
async def overview(
    # 日期范围控件的开始日期和结束日期，格式 YYYY-MM-DD，不传时统计最近三十天
    date_from: str | None = None,
    date_to: str | None = None,
    # 模型下拉框选中的配置主键，不传时统计全部模型
    model_config_id: int | None = None,
    # 当前用户从 Token 解析，管理员和员工的统计范围不同
    current_user: User = Depends(get_current_user),
):
    return Result.success(
        await build_observability(current_user, date_from, date_to, model_config_id)
    )
