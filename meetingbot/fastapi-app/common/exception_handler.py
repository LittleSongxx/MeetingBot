import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette import status
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)


# 自定义异常类
class CustomException(Exception):
    def __init__(self, message: str, code: str = "500"):
        # message 是给用户看的提示，前端直接弹出来
        self.message = message
        # code 默认 500 表示普通业务失败；登录失效传 401，无权限传 403
        self.code = code


def setup_exceptions(app: FastAPI):
    @app.exception_handler(CustomException)
    async def custom_exception_handler(request: Request, exc: CustomException):
        return JSONResponse(
            # http总是返回200
            status_code=status.HTTP_200_OK,
            # 把异常里的 code 和 message 放进统一结构，前端按 code 分支处理
            content={"code": exc.code, "msg": exc.message}
        )

    @app.exception_handler(RequestValidationError)
    async def validate_exception_handler(request: Request, exc: RequestValidationError):
        # 参数类型或结构不对时，先在后端控制台打出明细便于排查
        logger.warning("请求参数校验失败", extra={"errors": exc.errors()})
        """处理422异常"""
        # 返回统一格式
        # 页面上只显示一句笼统提示，不把字段级报错抛给用户
        return JSONResponse(
            # http总是返回200
            status_code=status.HTTP_200_OK,
            content={"code": "500", "msg": "请求参数错误"}
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        # 没被业务代码处理的异常，控制台打出原始信息
        logger.exception("未处理异常：%r", exc)
        """处理所有异常"""
        # 返回统一格式
        response = JSONResponse(
            # http总是返回200
            status_code=status.HTTP_200_OK,
            content={"code": "500", "msg": "系统错误"}
        )
        # 跨域响应头由 CORS 中间件统一负责，这里不再手写 *（同源部署时不应出现 ACAO）
        return response
