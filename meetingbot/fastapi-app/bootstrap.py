"""部署初始化脚本（容器启动时先于 uvicorn 执行）。

职责：
1. 按环境变量创建/更新管理员账号（APP_ADMIN_USERNAME / APP_ADMIN_PASSWORD）；
2. 把库里遗留的明文密码原地升级为 bcrypt；
3. 按环境变量 upsert 四条模型配置（TRANSCRIPTION / MINUTES / AGENT / SPEAKER），
   这样换机器部署时不需要手工在页面上一条条填 Key；
4. SEED_DEMO=1 时灌一套可直接演示的数据：部门、演示员工、一场会议和会议资料
   （素材来自镜像内的 demo_files/，复制到 files/ 后写 meeting_material 行）。

脚本可重复执行：所有写入都先查存在性，已存在则跳过或更新为最新配置。
"""

import asyncio
import hashlib
import logging
import os
import shutil
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from common.logging_conf import setup_logging
from common.security import hash_password, migrate_if_plaintext, verify_password
from common.times import now
from demo_seed import seed_ai_demo
from models import AiModelConfig, Department, Meeting, MeetingMaterial, MeetingParticipant, PromptTemplate, User
from settings import TORTOISE_ORM, _env_int
from tortoise import Tortoise

setup_logging()
logger = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
FILES_DIR = APP_DIR / "files"
DEMO_DIR = APP_DIR / "demo_files"
MATERIAL_DIR = FILES_DIR / "meeting_material"

DEMO_DEPARTMENTS = [
    ("产品研发部", "负责产品设计、研发和技术评审"),
    ("市场运营部", "负责市场活动和运营增长"),
]

# 演示会议里挂的资料：源文件在 demo_files/，第二个元素就是资料类型
DEMO_MATERIALS = [
    ("产品发布评审会议录音.mp3", "AUDIO"),
    ("产品发布评审会议片段.mp4", "VIDEO"),
    ("产品发布评审会议议程.docx", "ATTACHMENT"),
    ("产品发布评审会议纪要模板.pdf", "ATTACHMENT"),
    ("产品发布评审会议资料包.zip", "ATTACHMENT"),
    ("产品发布评审会议白板.png", "ATTACHMENT"),
]


async def ensure_admin() -> None:
    username = os.getenv("APP_ADMIN_USERNAME", "admin")
    password = os.getenv("APP_ADMIN_PASSWORD", "")
    if not password:
        logger.warning("未设置 APP_ADMIN_PASSWORD，跳过管理员初始化")
        return
    # 演示头像：首次创建或库里还没头像时补一个，避免顶部头像裂图
    avatar = None
    try:
        avatar_path = _copy_demo_asset("1.png")
        avatar = f"/files/download/{avatar_path.name}"
    except Exception:  # 素材缺失时不影响管理员创建
        logger.warning("管理员演示头像素材缺失，跳过头像初始化")

    user = await User.get_or_none(username=username)
    if user is None:
        await User.create(
            username=username, password=hash_password(password), name="系统管理员",
            role="ADMIN", status="NORMAL", avatar=avatar,
        )
        logger.info("已创建管理员账号", extra={"username": username})
        return
    # 密码没变就不重写：bcrypt 每次加盐都会改 password 行，进而改变 JWT 的 pv 指纹，
    # 导致每次重启都把已登录的管理员踢下线。只有 .env 里的口令真的变了才更新。
    values = {"role": "ADMIN", "status": "NORMAL"}
    if not verify_password(password, user.password):
        values["password"] = hash_password(password)
        logger.info("管理员口令已按 .env 更新", extra={"username": username})
    if avatar and not user.avatar:
        values["avatar"] = avatar
    await User.filter(id=user.id).update(**values)
    logger.info("已更新管理员账号", extra={"username": username})


async def migrate_passwords() -> None:
    """历史库里的明文密码升级为哈希（幂等）。"""
    migrated = 0
    for user in await User.all():
        if migrate_if_plaintext(user):
            await User.filter(id=user.id).update(password=user.password)
            migrated += 1
    if migrated:
        logger.info("已把遗留明文密码升级为 bcrypt", extra={"count": migrated})


async def upsert_model_configs() -> int:
    """首次部署按 env 建四条模型配置；已存在的配置以页面/数据库为准，不覆盖。

    这样管理员在「AI 模型配置」页面改的 Key、地址、模型名不会在重启后被 .env 冲掉；
    .env 只在库里还没有配置、或配置的 Key 为空时用来“补空”。
    """
    shared_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    asr_key = os.getenv("ASR_API_KEY", "").strip() or shared_key
    llm_key = os.getenv("LLM_API_KEY", "").strip() or shared_key
    dashscope_url = os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1").rstrip("/")
    llm_url = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
    llm_model = os.getenv("LLM_MODEL", "qwen-plus")
    asr_model = os.getenv("ASR_MODEL", "paraformer-v2")
    asr_timeout = _env_int("ASR_TIMEOUT_SECONDS", 600)
    llm_timeout = _env_int("LLM_TIMEOUT_SECONDS", 600)

    specs = [
        # name, model_type, provider, base_url, key, model, timeout
        ("百炼会议转写", "TRANSCRIPTION", "DASHSCOPE", dashscope_url, asr_key, asr_model, asr_timeout),
        ("百炼会议纪要", "MINUTES", "OPENAI_COMPATIBLE", llm_url, llm_key, llm_model, llm_timeout),
        ("百炼纪要自检", "AGENT", "OPENAI_COMPATIBLE", llm_url, llm_key, llm_model, llm_timeout),
        ("百炼说话人匹配", "SPEAKER", "OPENAI_COMPATIBLE", llm_url, llm_key, llm_model, llm_timeout),
    ]
    configured = 0
    for name, model_type, provider, base_url, key, model, timeout in specs:
        config = await AiModelConfig.filter(model_type=model_type).order_by("id").first()
        if config is None:
            # 首次部署：按 env 建一条；没给 Key 时保持禁用，避免建了任务直接失败
            await AiModelConfig.create(
                name=name, model_type=model_type, provider=provider,
                base_url=base_url, api_key=key, model_name=model,
                timeout_seconds=timeout, enabled=bool(key),
            )
            if not key:
                logger.warning("模型未配置 Key，保持禁用", extra={"model_type": model_type})
        elif not config.api_key and key:
            # 库里还没有 Key（页面也没填）时才用 env 补齐，不覆盖页面已有配置
            await AiModelConfig.filter(id=config.id).update(
                api_key=key,
                base_url=config.base_url or base_url,
                model_name=config.model_name or model,
                timeout_seconds=config.timeout_seconds or timeout,
                enabled=True,
            )
            logger.info("已用环境变量补齐模型 Key", extra={"model_type": model_type})
        elif config.api_key and key and config.api_key != key:
            logger.warning("模型配置已存在且与 .env 不同，保留页面配置", extra={"model_type": model_type})
        configured += 1
    # 同用途只保留 id 最小的一条启用配置（历史数据可能有多条）
    for model_type in {spec[1] for spec in specs}:
        rows = await AiModelConfig.filter(model_type=model_type, enabled=True).order_by("id")
        for stale in rows[1:]:
            await AiModelConfig.filter(id=stale.id).update(enabled=False)
    return configured


async def ensure_prompts() -> None:
    """初始化 SQL 已灌入 5 条模板；这里只做缺失告警，避免模板被误删后静默不可用。"""
    rows = await PromptTemplate.all().values_list("code", flat=True)
    missing = {"MEETING_MINUTES_CHUNK", "MEETING_MINUTES_MERGE", "MINUTES_REVIEW", "MINUTES_REFINE", "SPEAKER_MATCH"} - set(rows)
    if missing:
        logger.warning("Prompt 模板缺失，请检查 db/02-seed.sql", extra={"missing": sorted(missing)})
    elif not rows:
        logger.warning("Prompt 模板表为空")


def _copy_demo_asset(file_name: str) -> Path:
    """把镜像内的演示素材复制到可写目录；同名文件只复制一次。"""
    source = DEMO_DIR / file_name
    if not source.is_file():
        raise RuntimeError(f"演示素材不存在：{source}")
    FILES_DIR.mkdir(parents=True, exist_ok=True)
    target = FILES_DIR / file_name
    if not target.is_file():
        shutil.copy2(source, target)
    return target


async def seed_demo() -> None:
    """灌一套可直接演示的数据（幂等）。"""
    if os.getenv("SEED_DEMO", "0").strip() != "1":
        return
    demo_password = os.getenv("DEMO_PASSWORD", "123456")

    departments = {}
    for name, description in DEMO_DEPARTMENTS:
        department = await Department.get_or_none(name=name)
        if department is None:
            department = await Department.create(name=name, description=description, status="NORMAL")
        departments[name] = department

    # 演示账号：员工/运营，密码来自 DEMO_PASSWORD（部署后请及时修改）
    demo_users = [
        ("employee", "张晨", "back_end", departments["产品研发部"].id, "后端开发工程师", "3.png"),
        ("operator", "李悦", "operations", departments["市场运营部"].id, "运营专员", "4.png"),
    ]
    created_users = {}
    for username, name, _position, department_id, title, avatar_name in demo_users:
        user = await User.get_or_none(username=username)
        if user is None:
            avatar_path = _copy_demo_asset(avatar_name)
            user = await User.create(
                username=username, password=hash_password(demo_password), name=name,
                avatar=f"/files/download/{avatar_path.name}", role="EMPLOYEE",
                department_id=department_id, position=title, status="NORMAL",
            )
        created_users[username] = user

    admin = await User.get_or_none(username=os.getenv("APP_ADMIN_USERNAME", "admin"))
    host = created_users["employee"]
    meeting = await Meeting.get_or_none(meeting_no="MTG-DEMO-001")
    if meeting is None:
        # 演示数据里已经有转写/纪要/自检，会议状态改成已结束，时间也放到过去，
        # 避免出现“会议还没开始却有完整纪要”的语义矛盾
        start_time = now() - timedelta(hours=2)
        meeting = await Meeting.create(
            meeting_no="MTG-DEMO-001", title="产品发布评审会", location="三楼第一会议室",
            agenda="确认 V2.0 发布范围、灰度节奏与对外通稿时间",
            start_time=start_time, end_time=start_time + timedelta(hours=1),
            creator_id=host.id, host_id=host.id, status="FINISHED",
        )
    elif meeting.status == "SCHEDULED":
        await Meeting.filter(id=meeting.id).update(status="FINISHED")
    participants = [host, created_users["operator"]]
    if admin is not None:
        participants.append(admin)
    for index, participant in enumerate(participants):
        exists = await MeetingParticipant.filter(meeting_id=meeting.id, user_id=participant.id).exists()
        if not exists:
            await MeetingParticipant.create(
                meeting_id=meeting.id, user_id=participant.id,
                participant_role="HOST" if participant.id == host.id else "PARTICIPANT",
                response_status="ACCEPTED" if index < 2 else "PENDING",
            )

    # 演示资料：已经有同名资料时跳过
    MATERIAL_DIR.mkdir(parents=True, exist_ok=True)
    for file_name, material_type in DEMO_MATERIALS:
        exists = await MeetingMaterial.filter(meeting_id=meeting.id, file_name=file_name).exists()
        if exists:
            continue
        source = _copy_demo_asset(file_name)
        extension = source.suffix.lstrip(".").lower()
        storage_name = f"{uuid4().hex}.{extension}"
        target = MATERIAL_DIR / storage_name
        shutil.copy2(source, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        await MeetingMaterial.create(
            meeting_id=meeting.id, file_name=file_name, storage_name=storage_name,
            file_type=material_type, content_type=None, file_ext=extension,
            file_size=target.stat().st_size, file_hash=digest, uploader_id=host.id,
        )
    await seed_ai_demo(meeting, host, created_users["operator"])
    logger.info("演示数据已就绪", extra={"meeting_id": meeting.id})


async def main() -> None:
    await Tortoise.init(config=TORTOISE_ORM)
    try:
        await ensure_admin()
        await migrate_passwords()
        configured = await upsert_model_configs()
        await ensure_prompts()
        try:
            await seed_demo()
        except Exception:
            # 演示数据只是锦上添花，任何播种异常都不能阻断 uvicorn 启动
            logger.exception("演示数据初始化失败，已跳过（不影响服务启动）")
        logger.info("bootstrap 完成", extra={"model_configs": configured})
    finally:
        await Tortoise.close_connections()


if __name__ == "__main__":
    asyncio.run(main())
