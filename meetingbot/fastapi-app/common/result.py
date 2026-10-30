from typing import Any, List

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel


class Result(BaseModel):
    # 业务状态码，'200' 成功，'500' 业务失败，'401' 登录失效，'403' 无权限
    code: str
    # 提示文案，前端在 code 不是 '200' 时直接弹出这句话
    msg: str
    # 业务数据
    data: Any = None

    @staticmethod
    def success(data: Any = None):
        json_data = None
        if data is not None:
            # 把 ORM 对象、datetime 等类型转成可以序列化成 JSON 的结构
            json_data = jsonable_encoder(data)
        return Result(code="200", msg="请求成功", data=json_data)

    @staticmethod
    def error(msg: str = "请求失败"):
        return Result(code="500", msg=msg)


class PageInfo(BaseModel):
    # 符合条件的总条数，前端赋给 data.total
    total: int = 0
    # 当前页数据，前端赋给 data.tableData
    list: List[Any] = []
