import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

# 应用侧统一日志：uvicorn 自带 access/error log，这里给业务代码一个带上下文的 logger。
# 之前全局异常只 print(repr(exc))——无堆栈、无路径，排查全靠猜。
logger = logging.getLogger("aimeeting")
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)


# 自定义异常类
class CustomException(Exception):
    def __init__(self, message: str, code: str = "400"):
        # message 是给用户看的提示，前端直接弹出来
        self.message = message
        # code：400 业务失败（默认）/ 401 登录失效 / 403 无权限。
        # 与 HTTP 状态码同名同义——响应现在带真实状态码，网关与监控可按码分流。
        self.code = code


# CustomException.code → HTTP 状态码（防御式：未知 code 落 400 而不是 500，
# 让"业务上不通过"永远不会被误读成"服务端故障"）
_HTTP_BY_CODE = {"400": 400, "401": 401, "403": 403, "404": 404}


def _cors_headers() -> dict[str, str]:
    """兜底处理器不经过 CORS 中间件，需要手工补头，否则浏览器读不到响应体。
    来源与主 CORS 配置同源（CORS_ALLOW_ORIGINS，缺省 * 保持本地开发行为）。"""
    import os
    origin = os.getenv("CORS_ALLOW_ORIGINS", "*").strip() or "*"
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "*",
        "Access-Control-Allow-Headers": "*",
    }


def setup_exceptions(app: FastAPI):
    @app.exception_handler(CustomException)
    async def custom_exception_handler(request: Request, exc: CustomException):
        return JSONResponse(
            status_code=_HTTP_BY_CODE.get(exc.code, 400),
            # body 保持 {code, msg} 结构：前端既有 code 分支继续可用
            content={"code": exc.code, "msg": exc.message},
            headers=_cors_headers(),
        )

    @app.exception_handler(RequestValidationError)
    async def validate_exception_handler(request: Request, exc: RequestValidationError):
        # 字段级明细进日志（含请求路径），页面只给一句笼统提示
        logger.warning("422 %s %s: %s", request.method, request.url.path, exc.errors())
        return JSONResponse(
            status_code=422,
            content={"code": "422", "msg": "请求参数错误"},
            headers=_cors_headers(),
        )

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        # 未被业务代码处理的异常：完整堆栈进日志（此前只有 repr，无法定位）
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"code": "500", "msg": "系统错误"},
            headers=_cors_headers(),
        )
