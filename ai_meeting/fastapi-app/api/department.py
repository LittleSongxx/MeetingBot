from typing import List

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from common.auth import get_current_user, require_admin
from common.exception_handler import CustomException
from common.result import PageInfo, Result
from common.times import format_datetime
from models import Department, User

router = APIRouter(prefix="/department")


class DepartmentPayload(BaseModel):
    # from_attributes 允许直接用 ORM 对象构造这个模型
    model_config = ConfigDict(from_attributes=True)

    # 编辑时由前端回填，新增时不存在，后端据此区分插入还是更新
    id: int | None = None
    # 弹窗部门名称输入框
    name: str | None = None
    # 弹窗部门说明文本域
    description: str | None = None
    # 弹窗状态单选框
    status: str | None = None


async def department_dict(department: Department) -> dict:
    # 统计 user 表里 department_id 指向本部门的记录数，作为表格“员工人数”列
    employee_count = await User.filter(department_id=department.id).count()
    return {
        # 编辑弹窗回填、删除接口都用这个ID
        "id": department.id,
        # 表格“部门名称”列，也回填弹窗的名称输入框
        "name": department.name,
        # 表格“部门说明”列，也回填弹窗的说明文本域
        "description": department.description,
        # 表格“状态”列，页面按它显示绿色或红色标签
        "status": department.status,
        # 表格“员工人数”列
        "employee_count": employee_count,
        # 两个时间字段统一格式化成 2026-09-09 15:04:05 这样的字符串再返回
        "create_time": format_datetime(department.create_time),
        "update_time": format_datetime(department.update_time),
    }


async def validate_department(payload: DepartmentPayload):
    # 名称是页面上唯一的必填项，后端独立再校验一次
    if not payload.name:
        raise CustomException("部门名称不能为空")
    # 状态只允许页面单选框给出的两个取值，None 表示前端没提交这个字段
    if payload.status not in {None, "NORMAL", "DISABLED"}:
        raise CustomException("部门状态参数错误")
    # 按名称查重，同名部门在员工弹窗的部门下拉框里没法区分，所以不允许重复
    duplicate_query = Department.filter(name=payload.name)
    # 编辑时要把自己排除掉，否则只改说明不改名称时会误判成名称重复
    if payload.id is not None:
        duplicate_query = duplicate_query.exclude(id=payload.id)
    if await duplicate_query.exists():
        raise CustomException("部门名称重复")


@router.post("/add")
async def add(payload: DepartmentPayload, _: User = Depends(require_admin)):
    # 名称非空、名称不重复、状态合法，统一在这里校验
    await validate_department(payload)
    # 向 department 表插入一条新记录
    await Department.create(
        # 来自弹窗部门名称输入框
        name=payload.name,
        # 来自弹窗部门说明文本域
        description=payload.description,
        # 前端没提交状态时按正常处理，和数据库默认值一致
        status=payload.status or "NORMAL",
    )
    return Result.success()


@router.put("/update")
async def update(payload: DepartmentPayload, _: User = Depends(require_admin)):
    # 编辑必须带上部门ID
    if payload.id is None:
        raise CustomException("部门ID不能为空")
    # 部门可能已被别人删除，先确认还在
    if not await Department.filter(id=payload.id).exists():
        raise CustomException("部门不存在")
    # 和新增走同一套校验：名称非空、名称不重复、状态合法
    await validate_department(payload)
    # exclude_unset 只取前端真正提交过的字段，没提交的字段保持数据库原值
    # exclude id 是因为 id 用在 where 条件里，不作为更新内容
    update_data = payload.model_dump(exclude_unset=True, exclude={"id"})
    # 按主键更新这一条部门记录
    await Department.filter(id=payload.id).update(**update_data)
    return Result.success()


async def validate_delete(ids: List[int]):
    # 还有员工挂在待删部门下时不允许删除，避免 user.department_id 指向一个不存在的部门
    if await User.filter(department_id__in=ids).exists():
        raise CustomException("请先调整部门下的员工")


@router.delete("/deleteBatch")
# ids 来自前端 request.delete 配置项里的 data，也就是表格勾选出的 data.ids
async def delete_batch(ids: List[int], _: User = Depends(require_admin)):
    # 勾选的部门里只要有一个还挂着员工，整批都不删，页面弹出“请先调整部门下的员工”
    await validate_delete(ids)
    # 校验通过后按ID集合一次性删除 department 表里的这些记录
    await Department.filter(id__in=ids).delete()
    return Result.success()


@router.delete("/delete/{department_id}")
# department_id 来自请求地址 /department/delete/{id}，就是表格行删除图标传进来的 scope.row.id
async def delete(department_id: int, _: User = Depends(require_admin)):
    # 复用批量删除的校验函数，把单个ID包成列表传进去
    await validate_delete([department_id])
    # 没有员工挂在这个部门下时，按主键删除这一条部门记录；前端收到 code '200' 后调用 load 刷新表格
    await Department.filter(id=department_id).delete()
    return Result.success()


@router.get("/selectAll")
async def select_all(_: User = Depends(get_current_user)):
    # 只返回正常状态的部门，停用部门不再出现在任何下拉框里
    # 排序口径和列表一致，都按ID升序
    departments = await Department.filter(status="NORMAL").order_by("id")
    return Result.success([await department_dict(department) for department in departments])


@router.get("/selectPage")
async def select_page(
    # name 来自页面部门名称输入框，默认空串表示不限
    name: str = "",
    # status 来自页面状态下拉框，默认空串表示不限
    status: str = "",
    # page_num 来自 el-pagination 的当前页码
    page_num: int = 1,
    # page_size 来自页面固定的每页条数
    page_size: int = 10,
    # 部门管理只对管理员开放，这里用 require_admin 依赖拦住员工
    _: User = Depends(require_admin),
):
    # name 为空串时 __contains 相当于不加条件，非空时按部门名称模糊匹配
    query = Department.filter(name__contains=name)
    # 状态下拉框有值时才追加等值条件
    if status:
        query = query.filter(status=status)
    # 按ID升序，保证翻页结果稳定
    departments = await query.order_by("id").offset((page_num - 1) * page_size).limit(page_size)
    # 用同一份条件统计总条数，供前端分页条使用
    total = await query.count()
    return Result.success(
        PageInfo(
            total=total,
            # 逐条转换成页面需要的字段结构
            list=[await department_dict(department) for department in departments],
        )
    )
