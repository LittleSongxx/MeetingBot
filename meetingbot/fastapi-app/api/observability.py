from common.auth import get_current_user, require_admin
from common.result import PageInfo, Result
from fastapi import APIRouter, BackgroundTasks, Depends
from models import OnlineEvalCase, OnlineEvalRun, User
from pydantic import BaseModel
from services import online_eval_service
from services.observability_service import build_cost_report, build_observability

router = APIRouter(prefix="/observability")


class OnlineEvalPayload(BaseModel):
    sample_size: int = 20


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


@router.get("/cost")
async def cost(
    date_from: str | None = None,
    date_to: str | None = None,
    current_user: User = Depends(require_admin),
):
    return Result.success(await build_cost_report(date_from, date_to))


@router.post("/online-eval/run")
async def run_online_eval_endpoint(payload: OnlineEvalPayload,
                                   background: BackgroundTasks,
                                   current_user: User = Depends(require_admin)):
    run = await online_eval_service.create_run(current_user, payload.sample_size)
    background.add_task(online_eval_service.run_online_eval, run.id)
    return Result.success({"run_id": run.id, "status": run.status})


def _run_dict(run: OnlineEvalRun) -> dict:
    return {
        "id": run.id, "status": run.status, "sample_size": run.sample_size,
        "model_config_id": run.model_config_id, "started_at": run.started_at,
        "finished_at": run.finished_at, "summary_json": run.summary_json,
        "error_message": run.error_message,
    }


@router.get("/online-eval/runs")
async def list_online_eval_runs(page_num: int = 1, page_size: int = 20,
                                _: User = Depends(require_admin)):
    query = OnlineEvalRun.all()
    total = await query.count()
    rows = await query.order_by("-id").offset((page_num - 1) * page_size).limit(page_size)
    return Result.success(PageInfo(total=total, list=[_run_dict(row) for row in rows]))


@router.get("/online-eval/runs/{run_id}")
async def get_online_eval_run(run_id: int, _: User = Depends(require_admin)):
    run = await OnlineEvalRun.get_or_none(id=run_id)
    if run is None:
        return Result.error("在线评测运行不存在")
    cases = await OnlineEvalCase.filter(run_id=run_id).order_by("id")
    return Result.success({
        **_run_dict(run),
        "cases": [{
            "id": item.id, "question": item.question, "answer": item.answer,
            "scores_json": item.scores_json, "judge_model": item.judge_model,
            "error_message": item.error_message, "create_time": item.create_time,
            "citations_json": item.citations_json, "tools_json": item.tools_json,
        } for item in cases],
    })
