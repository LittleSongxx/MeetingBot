import hashlib
import zipfile

from docx import Document
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, UploadFile
from starlette.responses import FileResponse
from tortoise.exceptions import IntegrityError

from api.meeting import get_accessible_meeting
from common.auth import get_current_user
from common.exception_handler import CustomException
from common.result import PageInfo, Result
from common.times import format_datetime
from models import MeetingMaterial, TranscriptionTask, User

router = APIRouter(prefix="/meetingMaterial")

# 服务端扩展名→MIME 映射：下载内容类型只由扩展名决定（客户端声明的 content_type 不可信）
MATERIAL_MIME_BY_EXT = {
    "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4", "aac": "audio/aac",
    "flac": "audio/flac", "ogg": "audio/ogg", "amr": "audio/amr",
    "mp4": "video/mp4", "mov": "video/quicktime", "avi": "video/x-msvideo",
    "mkv": "video/x-matroska", "flv": "video/x-flv", "wmv": "video/x-ms-wmv",
    "doc": "application/msword", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint", "pdf": "application/pdf",
    "txt": "text/plain", "md": "text/plain", "csv": "text/csv",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
}
# docx 在线预览的大小上限（50MB）：全量解析型预览必须有界
MAX_PREVIEW_BYTES = 50 * 1024 * 1024
MATERIAL_DIR = Path(__file__).resolve().parent.parent / "files" / "meeting_material"
MAX_FILE_SIZE = 500 * 1024 * 1024
AUDIO_EXTENSIONS = {"mp3", "wav", "m4a", "aac", "flac", "ogg", "wma"}
VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm", "mpeg", "mpg"}
ATTACHMENT_EXTENSIONS = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md",
    "zip", "rar", "7z", "png", "jpg", "jpeg", "gif",
}


def get_file_type(file_ext: str) -> str:
    # 扩展名在录音集合里就是 AUDIO，页面上这一行会出现“发起转写”按钮
    if file_ext in AUDIO_EXTENSIONS:
        return "AUDIO"
    # 视频同样可以发起转写，第 8 章会先用 FFmpeg 抽出音轨
    if file_ext in VIDEO_EXTENSIONS:
        return "VIDEO"
    # 文档、压缩包、图片统一归为附件
    if file_ext in ATTACHMENT_EXTENSIONS:
        return "ATTACHMENT"
    # 三个集合都不命中说明格式不支持，和前端 allowedExtensions 的口径一致
    raise CustomException("不支持该文件格式")


def format_file_size(file_size: int) -> str:
    # 不到 1KB 直接显示字节数
    if file_size < 1024:
        return f"{file_size} B"
    # 不到 1MB 显示 KB，保留两位小数
    if file_size < 1024 * 1024:
        return f"{file_size / 1024:.2f} KB"
    # 其余显示 MB
    return f"{file_size / 1024 / 1024:.2f} MB"


async def material_dict(material: MeetingMaterial) -> dict:
    # 查上传人，用于返回姓名给列表展示
    uploader = await User.get_or_none(id=material.uploader_id)
    return {
        # 下载、删除、发起转写都用这个ID
        "id": material.id,
        "meeting_id": material.meeting_id,
        # 列表“文件名”列，也是下载时保存到本地用的名字
        "file_name": material.file_name,
        # 列表“资料类型”列，同时决定“发起转写”按钮是否出现
        "file_type": material.file_type,
        "content_type": material.content_type,
        "file_ext": material.file_ext,
        "file_size": material.file_size,
        # 列表“文件大小”列，由字节数换算成可读文本
        "file_size_text": format_file_size(material.file_size),
        "uploader_id": material.uploader_id,
        # 列表“上传人”列
        "uploader_name": uploader.name if uploader else None,
        # 列表“上传时间”列
        "create_time": format_datetime(material.create_time),
    }


async def get_accessible_material(material_id: int, current_user: User) -> MeetingMaterial:
    material = await MeetingMaterial.get_or_none(id=material_id)
    if material is None:
        raise CustomException("会议资料不存在")
    # 资料本身不单独管权限，先查出它属于哪场会议，再复用第 5 章的会议权限判断
    await get_accessible_meeting(material.meeting_id, current_user)
    return material


@router.post("/upload/{meeting_id}")
async def upload(
    # meeting_id 来自上传地址的路径参数
    meeting_id: int,
    # 浏览器以 multipart 表单提交，字段名和前端 formData.append('file', ...) 对应
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
):
    # meeting_id 来自上传地址，先校验登录用户能否访问该会议。
    await get_accessible_meeting(meeting_id, current_user)
    # 去掉路径部分只保留文件名，避免上传方带目录
    original_name = (file.filename or "").replace("\\", "/").split("/")[-1].strip()
    if not original_name or "." not in original_name:
        raise CustomException("文件名或扩展名不正确")
    # 取小写扩展名，用它判定资料类型
    file_ext = original_name.rsplit(".", 1)[-1].lower()
    file_type = get_file_type(file_ext)
    # 用随机串重命名后存盘，扩展名保留便于人工排查
    storage_name = f"{uuid4().hex}.{file_ext}"
    MATERIAL_DIR.mkdir(parents=True, exist_ok=True)
    file_path = MATERIAL_DIR / storage_name
    # 一边写盘一边算摘要，不需要为了判重再读一遍文件
    file_hash = hashlib.sha256()
    file_size = 0
    try:
        with file_path.open("wb") as target:
            # 每次读 1MB 写一段，500MB 的文件也不会一次读进内存
            while chunk := await file.read(1024 * 1024):
                file_size += len(chunk)
                # 超过上限时中断，最外层的 except 会删掉写了一半的文件
                if file_size > MAX_FILE_SIZE:
                    raise CustomException("单个文件不能超过500MB")
                file_hash.update(chunk)
                target.write(chunk)
        if file_size == 0:
            raise CustomException("不能上传空文件")
        digest = file_hash.hexdigest()
        # 同一会议使用 SHA-256 判断重复内容，重复上传直接返回已有记录。
        existed = await MeetingMaterial.get_or_none(meeting_id=meeting_id, file_hash=digest)
        if existed:
            # 已有同内容记录，删掉刚写的副本，把老记录返回给前端
            file_path.unlink(missing_ok=True)
            data = await material_dict(existed)
            # duplicated 为 true 时页面提示“该资料已存在”
            data["duplicated"] = True
            return Result.success(data)
        try:
            material = await MeetingMaterial.create(
                meeting_id=meeting_id,
                # 原始文件名入库，下载时按它命名
                file_name=original_name,
                # 磁盘上的实际文件名
                storage_name=storage_name,
                # 由扩展名判定出的三种类型之一
                file_type=file_type,
                # 浏览器带来的 MIME 类型，下载时按它设置响应类型
                content_type=file.content_type,
                file_ext=file_ext,
                file_size=file_size,
                file_hash=digest,
                # 上传人取自 Token 里的当前用户
                uploader_id=current_user.id,
            )
        except IntegrityError:
            # 并发上传相同文件时，唯一索引只保留一条记录。
            # 上面的查重和这里的插入之间可能被另一个请求插队，靠 uk_meeting_material_hash 兜底
            existed = await MeetingMaterial.get_or_none(meeting_id=meeting_id, file_hash=digest)
            if existed is None:
                raise
            file_path.unlink(missing_ok=True)
            data = await material_dict(existed)
            data["duplicated"] = True
            return Result.success(data)
        data = await material_dict(material)
        # 正常新增时 duplicated 为 false，页面提示“上传成功”
        data["duplicated"] = False
        return Result.success(data)
    except Exception:
        # 任何一步失败都清掉落在磁盘上的残留文件，不留下没有数据库记录的孤儿文件
        file_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()


@router.get("/selectPage")
async def select_page(
    # meetingId 来自页面顶部的会议下拉框，是必传参数
    meetingId: int,
    # fileName 来自文件名输入框
    fileName: str = "",
    # fileType 来自资料类型下拉框
    fileType: str = "",
    pageNum: int = 1,
    pageSize: int = 10,
    current_user: User = Depends(get_current_user),
):
    # 分页查询与上传、下载共用同一会议数据权限校验。
    await get_accessible_meeting(meetingId, current_user)
    # 限定在这场会议下，再按文件名模糊匹配
    query = MeetingMaterial.filter(meeting_id=meetingId, file_name__contains=fileName)
    # 资料类型有值时追加等值条件
    if fileType:
        query = query.filter(file_type=fileType)
    # 按上传时间倒序，最新上传的排在最前面
    materials = await query.order_by("-create_time", "-id").offset(
        (pageNum - 1) * pageSize
    ).limit(pageSize)
    total = await query.count()
    return Result.success(
        PageInfo(total=total, list=[await material_dict(item) for item in materials])
    )


@router.get("/zipEntries/{material_id}")
async def zip_entries(material_id: int, current_user: User = Depends(get_current_user)):
    """读取压缩包内部的文件清单，页面上直接展示，不解压也不读取文件内容。"""
    # material_id 来自请求地址；先查资料，再按所属会议校验当前用户的访问权限
    material = await get_accessible_material(material_id, current_user)
    # 从原始文件名取小写后缀
    extension = material.file_name.rsplit(".", 1)[-1].lower() if "." in material.file_name else ""
    # 只有 zip 能读，rar 和 7z 需要额外的解压程序，这里不支持
    if extension != "zip":
        raise CustomException("只支持查看 ZIP 压缩包里的文件清单")
    # 磁盘上的实际文件用 storage_name 定位
    file_path = MATERIAL_DIR / material.storage_name
    if not file_path.is_file():
        raise CustomException("文件不存在")
    entries = []
    try:
        # 标准库 zipfile 只读取压缩包目录，不解压文件内容
        with zipfile.ZipFile(file_path) as zf:
            # 最多列 500 条，压缩包里文件太多时页面也放不下
            for info in zf.infolist()[:500]:
                name = info.filename
                # 标志位第 11 位表示文件名是 UTF-8；没置位的压缩包多半是 Windows 下打的，按 GBK 还原
                if not info.flag_bits & 0x800:
                    try:
                        name = name.encode("cp437").decode("gbk")
                    except (UnicodeDecodeError, UnicodeEncodeError):
                        # 按 GBK 也还原不了时保留 zipfile 读出的原始名字
                        pass
                entries.append({
                    # 清单表格“文件名”列
                    "name": name,
                    # 目录项的文件名以斜杠结尾，页面据此显示成文件夹
                    "is_dir": name.endswith("/"),
                    # 文件解压后的原始字节数，前端 sizeText 换算后显示在“大小”列
                    "size": info.file_size,
                })
    except zipfile.BadZipFile:
        raise CustomException("压缩包已损坏，无法读取文件清单")
    # 前端把 entries 写入 preview.entries 后打开弹窗
    return Result.success({"file_name": material.file_name, "entries": entries})


@router.get("/docxPreview/{material_id}")
async def docx_preview(material_id: int, current_user: User = Depends(get_current_user)):
    """读取 Word 文档里的文字，页面上按段落展示。浏览器打不开 docx，只能这样呈现内容。"""
    # material_id 来自请求地址；先查资料，再按所属会议校验当前用户的访问权限
    material = await get_accessible_material(material_id, current_user)
    # 从原始文件名取小写后缀
    extension = material.file_name.rsplit(".", 1)[-1].lower() if "." in material.file_name else ""
    # 老版本的 doc 是二进制格式，python-docx 读不了
    if extension != "docx":
        raise CustomException("只支持查看 DOCX 文档的内容")
    # 磁盘上的实际文件用 storage_name 定位
    file_path = MATERIAL_DIR / material.storage_name
    if not file_path.is_file():
        raise CustomException("文件不存在")
    # 预览是全量解析，给一个大小上限防畸形/超大文档耗尽 CPU 与内存
    if file_path.stat().st_size > MAX_PREVIEW_BYTES:
        raise CustomException("文档过大，请下载后本地查看")
    try:
        # python-docx 打开文档；文件内容不是合法的 docx 时会抛异常
        document = Document(str(file_path))
    except Exception:
        raise CustomException("文档已损坏，无法读取内容")

    blocks = []
    for paragraph in document.paragraphs:
        text = paragraph.text.strip()
        # 空段落不返回，页面上不会出现一串空行
        if not text:
            continue
        # 段落套用了标题样式时，页面上用大一号的字重显示
        style_name = (paragraph.style.name or "").lower()
        blocks.append({
            # 样式名以 heading 或 title 开头记为 heading，其余记为 text，前端按它选择显示样式
            "type": "heading" if style_name.startswith("heading") or style_name.startswith("title") else "text",
            "text": text,
        })
    # 表格单独取出来，页面按行列还原成一张表
    for table in document.tables:
        rows = []
        for row in table.rows:
            # 每一行是一个字符串数组，前端把第一行当表头
            rows.append([cell.text.strip() for cell in row.cells])
        if rows:
            blocks.append({"type": "table", "rows": rows})
    # 最多返回 500 块，前端写入 preview.blocks 后打开弹窗
    return Result.success({"file_name": material.file_name, "blocks": blocks[:500]})


@router.get("/download/{material_id}")
async def download(material_id: int, current_user: User = Depends(get_current_user)):
    # material_id 先定位资料，再通过 meeting_id 校验所属会议权限。
    material = await get_accessible_material(material_id, current_user)
    # 磁盘上的实际文件按 storage_name 定位
    file_path = MATERIAL_DIR / material.storage_name
    if not file_path.is_file():
        raise CustomException("文件不存在")
    # 安全：MIME 由服务端按扩展名映射，不回放上传方声明的 content_type
    #（那是客户端可控字段——声明 text/html 的"txt"会在同源代理下执行脚本）。
    # 下载一律 attachment + nosniff：浏览器只许落盘，不许当页面渲染。
    from urllib.parse import quote
    response = FileResponse(
        path=file_path,
        filename=material.file_name,
        media_type=MATERIAL_MIME_BY_EXT.get(
            (material.file_ext or "").lower(), "application/octet-stream"),
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    encoded_name = quote(material.file_name)
    response.headers["Content-Disposition"] = (
        f"attachment; filename*=UTF-8''{encoded_name}")
    return response


async def delete_material(material: MeetingMaterial):
    # 第 8 章的转写任务记着 material_id，资料被删会让转写记录指向不存在的文件
    if await TranscriptionTask.filter(material_id=material.id).exists():
        raise CustomException("该资料已关联转写任务，不能删除")
    # 先删磁盘文件，missing_ok 让文件已经不在时也不报错
    file_path = MATERIAL_DIR / material.storage_name
    file_path.unlink(missing_ok=True)
    # 再删数据库记录
    await MeetingMaterial.filter(id=material.id).delete()


@router.delete("/delete/{material_id}")
async def delete(material_id: int, current_user: User = Depends(get_current_user)):
    # 先按权限取资料，看不到的资料删不了
    material = await get_accessible_material(material_id, current_user)
    # 删除是硬删 + 清磁盘，口径与转写任务删除对齐：管理员或上传人
    #（此前任何参会人都能删别人的录音，且不可恢复）
    if current_user.role != "ADMIN" and material.uploader_id != current_user.id:
        raise CustomException("只有管理员或上传人可以删除该资料", "403")
    await delete_material(material)
    return Result.success()


@router.delete("/deleteBatch")
async def delete_batch(ids: list[int], current_user: User = Depends(get_current_user)):
    # 前端已经拦了空选，后端再确认一次
    if not ids:
        raise CustomException("请选择要删除的会议资料")
    materials = []
    # set 去重后逐个做权限校验，勾选里混进无权访问的资料时整批拒绝
    for material_id in set(ids):
        material = await get_accessible_material(material_id, current_user)
        # 批量删除同样只允许管理员或上传人清理自己的文件
        if current_user.role != "ADMIN" and material.uploader_id != current_user.id:
            raise CustomException("勾选中包含他人上传的资料，不能删除", "403")
        materials.append(material)
    # 整批一次性检查转写关联，只要有一份被关联就全部不删，避免删一半停下来
    if await TranscriptionTask.filter(material_id__in=[item.id for item in materials]).exists():
        raise CustomException("勾选资料中存在已关联转写任务的文件，不能删除")
    for material in materials:
        await delete_material(material)
    return Result.success()
