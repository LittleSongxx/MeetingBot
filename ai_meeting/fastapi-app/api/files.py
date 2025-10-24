from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile
from starlette.responses import FileResponse

from common.auth import get_current_user
from common.exception_handler import CustomException
from common.result import Result
from models import User

UPLOAD_DIR = Path(__file__).resolve().parent.parent / "files"
AVATAR_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp"}
MAX_AVATAR_SIZE = 5 * 1024 * 1024
router = APIRouter(prefix="/files")


@router.post("/upload")
async def upload_file(
    # 浏览器以 multipart 表单方式提交，参数名固定为 file
    file: UploadFile = File(...),
    # 要求登录，Token 从请求头读取，也就是 el-upload 的 :headers 带上的那个
    _: User = Depends(get_current_user),
):
    # 去掉路径部分只保留文件名，避免上传方带目录
    original_name = (file.filename or "").replace("\\", "/").split("/")[-1].strip()
    # 没有文件名或没有扩展名时无法判断类型，直接拒绝
    if not original_name or "." not in original_name:
        raise CustomException("头像文件名不正确")
    # 取最后一段扩展名并转小写，用于比对白名单
    extension = original_name.rsplit(".", 1)[-1].lower()
    if extension not in AVATAR_EXTENSIONS:
        raise CustomException("头像只支持JPG、PNG、GIF和WEBP图片")
    # 首次上传时创建存放目录
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    # 用随机串重命名，避免不同用户上传同名文件时互相覆盖
    storage_name = f"{uuid4().hex}.{extension}"
    file_path = UPLOAD_DIR / storage_name
    file_size = 0
    try:
        with file_path.open("wb") as target:
            # 每次读 1MB 写一段，边读边累计大小，不把整个文件读进内存
            while chunk := await file.read(1024 * 1024):
                file_size += len(chunk)
                # 超过限制时中断，下面的 except 会把已写入的半个文件删掉
                if file_size > MAX_AVATAR_SIZE:
                    raise CustomException("头像文件不能超过5MB")
                target.write(chunk)
        if file_size == 0:
            raise CustomException("不能上传空头像文件")
    except Exception:
        # 任何一步失败都清掉落在磁盘上的残留文件
        file_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()
    # 返回可直接访问的下载地址，前端把它写进 data.user.avatar
    return Result.success("/files/download/" + storage_name)


# 文件下载
@router.get("/download/{filename}")
async def download_file(filename: str):
    # Path(filename).name 会剥掉目录部分，两者不相等说明请求里带了路径，直接拒绝
    if Path(filename).name != filename:
        raise CustomException("文件名不正确")
    # 本目录只存放头像：按扩展名映射图片 MIME（不信文件内容，也不存在用户可控的
    # content_type 字段），未知扩展名一律二进制流。头像经 <img> 加载无法带 token，
    # 因此保持公开，但内容类型被钉死 + nosniff，不可被当 HTML 执行。
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    image_types = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                   "gif": "image/gif", "webp": "image/webp"}
    response = FileResponse(
        path=UPLOAD_DIR / filename,
        filename=filename,
        media_type=image_types.get(extension, "application/octet-stream"),
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response
