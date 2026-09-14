import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx

from api.meeting_material import MATERIAL_DIR
from common.times import now
from models import (
    AiCallLog,
    AiModelConfig,
    MeetingMaterial,
    TranscriptionTask,
    TranscriptSegment,
)
from settings import CHUNK_SECONDS

logger = logging.getLogger(__name__)

_running_tasks: dict[int, asyncio.Task] = {}
_semaphore: asyncio.Semaphore | None = None


def get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    # 第一次用到时才创建，保证它绑定在应用运行的那个事件循环上
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(2)
    return _semaphore


async def prepare_audio_chunks(source_path: Path, output_dir: Path) -> list[Path]:
    # 没装 FFmpeg 时直接给出明确提示，而不是让 ffmpeg 命令报找不到
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("未找到FFmpeg，请先安装并加入PATH")
    # 分段文件名模板，%03d 由 FFmpeg 按段号填充，得到 chunk_000.mp3、chunk_001.mp3
    output_pattern = output_dir / "chunk_%03d.mp3"
    command = [
        "ffmpeg",
        # 不打印版本横幅，只保留错误信息，便于失败时把 stderr 当作原因
        "-hide_banner",
        "-loglevel",
        "error",
        # 输出文件已存在时直接覆盖，不等待交互确认
        "-y",
        "-i",
        str(source_path),
        # 丢掉视频轨，视频文件也只取声音
        "-vn",
        # 单声道
        "-ac",
        "1",
        # 16kHz 采样率
        "-ar",
        "16000",
        # 64kbps 码率
        "-b:a",
        "64k",
        # 按分段方式输出
        "-f",
        "segment",
        # 每段 1200 秒，也就是 20 分钟
        "-segment_time",
        str(CHUNK_SECONDS),
        # 每段时间戳从 0 重新开始，配合代码里的 offset_ms 计算绝对时间
        "-reset_timestamps",
        "1",
        str(output_pattern),
    ]
    # FFmpeg 放到线程里同步执行。asyncio 的子进程接口在 Windows 上依赖 Proactor 事件循环，
    # 而 uvicorn 启动时会把事件循环换成 Selector 版，直接调用会抛出没有任何说明的 NotImplementedError。
    completed = await asyncio.to_thread(
        subprocess.run,
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(message or "FFmpeg音轨提取失败")
    # 按文件名排序，保证分段顺序和时间顺序一致
    chunks = sorted(output_dir.glob("chunk_*.mp3"))
    # 一个分段都没有，说明源文件里没有可用音轨
    if not chunks:
        raise RuntimeError("未从会议资料中识别到可转写音轨")
    return chunks


async def create_call_log(
    task: TranscriptionTask,
    config: AiModelConfig,
    request_size: int,
    response_size: int,
    status: str,
    elapsed_ms: int,
    usage: dict | None = None,
    error_message: str | None = None,
):
    # 每个分段的一次识别对应 ai_call_log 表的一行
    await AiCallLog.create(
        # 调用唯一编号，AIC 前缀加 32 位十六进制随机串
        request_id="AIC" + uuid4().hex.upper(),
        # 固定为 TRANSCRIPTION，第 17 章按它区分调用类型
        call_type="TRANSCRIPTION",
        # 业务ID是转写任务ID
        biz_id=task.id,
        # 模型配置ID和模型名，第 17 章的模型排行按它们分组显示
        model_config_id=config.id,
        model_name=config.model_name,
        request_size=request_size,
        response_size=response_size,
        # SUCCEEDED 或 FAILED
        status=status,
        elapsed_ms=elapsed_ms,
        # 成功时存百炼结果里的 properties，失败时为 None
        usage_json=usage,
        # 失败时存异常信息，成功时为 None
        error_message=error_message,
    )


# 单个分段的远端状态查询间隔和次数上限
POLL_INTERVAL_SECONDS = 5
POLL_MAX_TIMES = 240


async def upload_audio_to_dashscope(
    client: httpx.AsyncClient,
    config: AiModelConfig,
    audio_path: Path,
) -> str:
    """把本地音频传到百炼的临时文件空间，换回一个 oss:// 地址。

    百炼的录音文件识别只接受可访问的音频地址，不能像 OpenAI 那样把文件直接贴在请求体里，
    所以先走一趟上传：向百炼要一份 OSS 上传凭证，按凭证把文件 POST 到 OSS，再拿地址去提交转写。
    """
    # 向配置的百炼基础地址申请当前模型的临时音频上传凭证。
    policy_response = await client.get(
        config.base_url.rstrip("/") + "/uploads",
        params={"action": "getPolicy", "model": config.model_name},
        headers={"Authorization": f"Bearer {config.api_key}"},
    )
    if policy_response.status_code >= 400:
        raise RuntimeError(f"申请音频上传凭证失败：{policy_response.text[:500]}")
    policy = (policy_response.json() or {}).get("data") or {}
    for field in ("upload_host", "upload_dir", "policy", "signature", "oss_access_key_id"):
        if not policy.get(field):
            raise RuntimeError("音频上传凭证返回格式不正确")

    # OSS 里的完整对象名，upload_dir 由百炼分配，文件名沿用分段音频的名字
    # 上传目录来自凭证，分段文件名来自 FFmpeg；拼出的对象键用于后续转写请求。
    object_key = f"{policy['upload_dir']}/{audio_path.name}"
    form = {
        "OSSAccessKeyId": policy["oss_access_key_id"],
        "policy": policy["policy"],
        "Signature": policy["signature"],
        "key": object_key,
        "x-oss-object-acl": policy.get("x_oss_object_acl", ""),
        "x-oss-forbid-overwrite": policy.get("x_oss_forbid_overwrite", ""),
        "success_action_status": "200",
    }
    with audio_path.open("rb") as audio_file:
        # 上传 MP3 文件字节，上传表单中的签名字段全部来自刚取得的凭证。
        upload_response = await client.post(
            policy["upload_host"],
            data={key: value for key, value in form.items() if value != ""},
            files={"file": (audio_path.name, audio_file, "audio/mpeg")},
        )
    if upload_response.status_code >= 400:
        raise RuntimeError(f"音频上传失败：{upload_response.text[:500]}")
    return f"oss://{object_key}"


async def submit_dashscope_transcription(
    client: httpx.AsyncClient,
    task: TranscriptionTask,
    config: AiModelConfig,
    file_url: str,
) -> str:
    """提交异步转写任务，拿回任务ID。diarization_enabled 打开后模型才会给出说话人编号。"""
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
        # 告诉百炼这是异步任务，接口立刻返回任务ID而不是等转写结束
        "X-DashScope-Async": "enable",
        # 音频地址是上一步换来的 oss:// 内部地址，带上这个头百炼才会去解析它
        "X-DashScope-OssResourceResolve": "enable",
    }
    # 构造本次识别参数；说话人分离结果随后保存到 transcript_segment.speaker_label。
    parameters = {
        # 打开说话人分离，返回的句子里才有 speaker_id
        "diarization_enabled": True,
    }
    # language 来自资料页的语言下拉框；自动识别对应空字符串，不添加语言提示。
    if task.language:
        # 页面选了具体语言时作为提示传过去，省掉模型自己判断语种的环节
        parameters["language_hints"] = [task.language]
    body = {
        "model": config.model_name,
        "input": {"file_urls": [file_url]},
        "parameters": parameters,
    }
    response = await client.post(
        config.base_url.rstrip("/") + "/services/audio/asr/transcription",
        headers=headers,
        json=body,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"转写模型请求失败：{response.text[:1000]}")
    # 提取百炼异步任务编号，交给 wait_dashscope_result 查询远端执行状态。
    task_id = ((response.json() or {}).get("output") or {}).get("task_id")
    if not task_id:
        raise RuntimeError("转写模型未返回任务ID")
    return task_id


async def wait_dashscope_result(
    client: httpx.AsyncClient,
    config: AiModelConfig,
    dashscope_task_id: str,
) -> dict:
    """按固定间隔查询任务状态，成功后把转写结果的 JSON 下载回来。"""
    # 每个音频分段最多查询 240 次，每次查询前等待 5 秒。
    for _ in range(POLL_MAX_TIMES):
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        response = await client.get(
            config.base_url.rstrip("/") + f"/tasks/{dashscope_task_id}",
            headers={"Authorization": f"Bearer {config.api_key}"},
        )
        if response.status_code >= 400:
            raise RuntimeError(f"查询转写任务失败：{response.text[:500]}")
        output = (response.json() or {}).get("output") or {}
        status = output.get("task_status")
        if status == "SUCCEEDED":
            results = output.get("results") or []
            if not results:
                raise RuntimeError("转写任务没有返回结果")
            first = results[0]
            # 单个音频自身也可能失败，这时结果里带的是错误信息而不是结果地址
            if first.get("subtask_status") == "FAILED" or not first.get("transcription_url"):
                raise RuntimeError("转写失败：" + str(first.get("message") or "未返回结果地址"))
            # 成功任务给出转写 JSON 地址，下载后交给 convert_dashscope_result 统一字段。
            detail = await client.get(first["transcription_url"])
            if detail.status_code >= 400:
                raise RuntimeError("下载转写结果失败")
            return detail.json() or {}
        if status == "FAILED":
            reason = output.get("message") or output.get("code") or "模型未说明原因"
            raise RuntimeError("转写失败：" + str(reason))
    raise RuntimeError("转写任务超时未完成")


def convert_dashscope_result(detail: dict) -> dict:
    """把百炼的转写结果转换成本项目内部统一的形状。

    转换之后的字典和 parse_segments 期望的结构一致：
    text 是整段文字，duration 是秒数，segments 里每条带 start、end、speaker 和 text。
    """
    transcripts = detail.get("transcripts") or []
    segments = []
    texts = []
    duration_ms = 0
    for transcript in transcripts:
        if str(transcript.get("text") or "").strip():
            texts.append(str(transcript["text"]).strip())
        duration_ms = max(duration_ms, int(transcript.get("content_duration_in_milliseconds") or 0))
        for sentence in transcript.get("sentences") or []:
            content = str(sentence.get("text") or "").strip()
            if not content:
                continue
            # 每句文本转成内部 segments 项，parse_segments 随后叠加分段时间偏移。
            segments.append({
                # 百炼给的是毫秒，这里统一换成秒，和 parse_segments 的换算口径对上
                "start": int(sentence.get("begin_time") or 0) / 1000,
                "end": int(sentence.get("end_time") or sentence.get("begin_time") or 0) / 1000,
                # 说话人编号，没开分离或全程只有一个人时百炼不返回这个字段
                "speaker": sentence.get("speaker_id"),
                "text": content,
            })
    return {
        "text": "\n".join(texts),
        "duration": duration_ms / 1000,
        "segments": segments,
    }


async def call_transcription_model(
    task: TranscriptionTask,
    config: AiModelConfig,
    audio_path: Path,
) -> dict:
    # 记录开始时刻，成功或失败时都用它计算 elapsed_ms
    started = perf_counter()
    # 请求大小取这个分段 MP3 的字节数
    request_size = audio_path.stat().st_size
    response_size = 0
    try:
        # timeout 取第 7 章配置的超时秒数，三步请求共用这一个客户端
        async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
            # 第一步：本地音频换成百炼能访问的地址
            file_url = await upload_audio_to_dashscope(client, config, audio_path)
            # 第二步：提交异步转写任务
            dashscope_task_id = await submit_dashscope_transcription(client, task, config, file_url)
            # 第三步：轮询到成功后把结果 JSON 取回来
            detail = await wait_dashscope_result(client, config, dashscope_task_id)
        # 结果 JSON 转成字符串后的长度记为响应大小，写进 ai_call_log.response_size
        response_size = len(str(detail))
        # 百炼的 transcripts、sentences 结构转换成 parse_segments 使用的 text、duration、segments
        result = convert_dashscope_result(detail)
        # 既没有句子也没有整段文字，按失败处理，进入下面的 except 分支
        if not result["segments"] and not result["text"]:
            raise RuntimeError("转写模型返回格式不正确")
        # 从上传开始到拿到结果的总耗时，单位毫秒
        elapsed_ms = int((perf_counter() - started) * 1000)
        # 成功日志：biz_id 是转写任务ID，第 17 章员工视角按发起人筛选时靠它归属
        await create_call_log(
            task,
            config,
            request_size,
            response_size,
            "SUCCEEDED",
            elapsed_ms,
            # 将百炼 properties 原样存入 ai_call_log.usage_json。
            usage=detail.get("properties"),
        )
        return result
    except Exception as error:
        elapsed_ms = int((perf_counter() - started) * 1000)
        # 上传、提交、轮询、解析任何一步抛异常都记一条失败日志，第 17 章据此统计失败次数
        await create_call_log(
            task,
            config,
            request_size,
            response_size,
            "FAILED",
            elapsed_ms,
            error_message=(str(error) or type(error).__name__)[:2000],
        )
        raise


def parse_segments(
    result: dict,
    # 本分段在整段音频里的起始毫秒
    offset_ms: int,
    # 本分段第一个片段的全局序号
    start_no: int,
    # 说话人标签前缀，取分段序号，形如 01
    speaker_prefix: str,
) -> tuple[list[dict], int, int]:
    response_segments = result.get("segments") or []
    parsed = []
    max_end_ms = 0
    for _index, item in enumerate(response_segments):
        content = str(item.get("text") or "").strip()
        # 跳过空白片段，不占用序号
        if not content:
            continue
        # 模型返回的是秒，乘 1000 转成毫秒
        start_ms = int(float(item.get("start") or 0) * 1000)
        # 没有结束时间时退回用开始时间，保证字段有值
        end_ms = int(float(item.get("end") or item.get("start") or 0) * 1000)
        # 记住本段里最大的结束时间，用于兜底计算本段时长
        max_end_ms = max(max_end_ms, end_ms)
        # 百炼的说话人编号从 0 开始，0 是有效编号，只有字段缺失时才用 A 代替
        speaker = item.get("speaker")
        parsed.append(
            {
                # 序号按已解析出的条数递增，保证连续
                "segment_no": start_no + len(parsed),
                # 加上偏移后是整段音频里的绝对时间
                "start_ms": offset_ms + start_ms,
                "end_ms": offset_ms + end_ms,
                # 加分段前缀，每个分段的说话人编号都从 0 开始，01-0 和 02-0 不会被当成同一个人
                "speaker_label": speaker_prefix + "-" + (str(speaker) if speaker is not None else "A"),
                # 原始文本和可修订文本先写成一样，第 9 章修订时只改 content
                "original_text": content,
                "content": content,
            }
        )
    # 优先用模型返回的分段总时长
    duration_ms = int(float(result.get("duration") or 0) * 1000)
    # 模型没给时长或给得偏小时，用片段里最大的结束时间兜底
    duration_ms = max(duration_ms, max_end_ms)
    # 模型只给了整段文本没给分段时，把整段文本当作一个片段
    if not parsed and str(result.get("text") or "").strip():
        content = str(result["text"]).strip()
        parsed.append(
            {
                "segment_no": start_no,
                "start_ms": offset_ms,
                "end_ms": offset_ms + duration_ms,
                "speaker_label": speaker_prefix + "-A",
                "original_text": content,
                "content": content,
            }
        )
    # 依次返回：本段解析出的片段、本段时长、下一段可用的起始序号
    return parsed, duration_ms, start_no + len(parsed)


async def execute_transcription(task_id: int):
    # 用带条件的 update 抢占任务：只有还处于 PENDING 的任务才能被改成 PROCESSING
    # 返回受影响行数为 0，说明已经有别的协程接手了，本次直接退出
    updated = await TranscriptionTask.filter(id=task_id, status="PENDING").update(
        status="PROCESSING",
        stage="正在准备音频",
        progress=5,
        error_message=None,
        started_at=now(),
        finished_at=None,
    )
    if updated == 0:
        return
    task = await TranscriptionTask.get(id=task_id)
    logger.info("转写任务开始", extra={"task_id": task.id, "material_id": task.material_id})
    try:
        # 资料可能在排队期间被删掉
        material = await MeetingMaterial.get_or_none(id=task.material_id)
        if material is None:
            raise RuntimeError("转写的会议资料不存在")
        # 模型配置可能在排队期间被改动
        config = await AiModelConfig.get_or_none(id=task.model_config_id)
        if config is None or not config.api_key:
            raise RuntimeError("转写模型配置不存在或API Key为空")
        # 按第 6 章存下的 storage_name 定位磁盘文件
        source_path = MATERIAL_DIR / material.storage_name
        if not source_path.is_file():
            raise RuntimeError("转写的音视频文件不存在")
        # 临时目录存放切好的音频段，with 结束时自动整个删掉
        with tempfile.TemporaryDirectory(prefix="ai_meeting_transcribe_") as temp_dir:
            chunks = await prepare_audio_chunks(source_path, Path(temp_dir))
            # 音频准备完成，进度推到 20
            await TranscriptionTask.filter(id=task.id).update(
                stage=f"音频处理完成，共{len(chunks)}个分段",
                progress=20,
            )
            all_segments = []
            full_text_parts = []
            # 当前分段在整段音频里的起始毫秒，逐段累加
            offset_ms = 0
            # 全局片段序号，跨分段连续编号
            segment_no = 1
            for index, chunk in enumerate(chunks):
                # 每开始一段就把阶段和进度写库，页面轮询能看到变化
                # 20 到 90 这 70 个百分点按分段数均分
                await TranscriptionTask.filter(id=task.id).update(
                    stage=f"正在转写第{index + 1}/{len(chunks)}个分段",
                    progress=20 + int(index * 70 / len(chunks)),
                )
                result = await call_transcription_model(task, config, chunk)
                # 把这一段的结果解析成片段列表，并返回本段时长和下一个可用序号
                parsed, duration_ms, segment_no = parse_segments(
                    result, offset_ms, segment_no, f"{index + 1:02d}"
                )
                all_segments.extend(parsed)
                if str(result.get("text") or "").strip():
                    full_text_parts.append(str(result["text"]).strip())
                # 下一段的时间偏移等于前面所有段的时长之和
                offset_ms += duration_ms
            # 一段有效文本都没有时按失败处理，不写空结果
            if not all_segments:
                raise RuntimeError("转写模型未返回有效文本")
            # 重试场景下可能已有上一次写入的片段，先清空再写
            await TranscriptSegment.filter(task_id=task.id).delete()
            await TranscriptSegment.bulk_create(
                [
                    TranscriptSegment(
                        task_id=task.id,
                        meeting_id=task.meeting_id,
                        # parse_segments 已经把每个片段的字段准备好，这里直接展开
                        **segment,
                    )
                    for segment in all_segments
                ]
            )
            logger.info("转写任务完成", extra={"task_id": task.id, "segments": len(all_segments), "chunks": len(chunks)})
            await TranscriptionTask.filter(id=task.id).update(
                status="SUCCEEDED",
                stage="转写完成",
                progress=100,
                # 全部分段时长之和就是音频总时长
                total_duration_ms=offset_ms,
                # 各段整体文本按换行拼接，第 14 章纪要自检审查时读取它
                full_text="\n".join(full_text_parts),
                error_message=None,
                finished_at=now(),
            )
    except Exception as error:
        logger.exception("转写任务失败", extra={"task_id": task_id})
        # 任何一步失败都把任务标成 FAILED 并记下原因，页面上出现“重试”按钮
        await TranscriptionTask.filter(id=task.id).update(
            status="FAILED",
            stage="转写失败",
            error_message=str(error)[:2000],
            finished_at=now(),
        )


async def run_scheduled(task_id: int):
    # 拿到信号量才真正开始执行，拿不到就在这里挂起排队
    async with get_semaphore():
        await execute_transcription(task_id)


def schedule_transcription(task_id: int):
    # 这个任务已经有一个协程在跑就不重复排，避免同一份音频被转两遍
    running = _running_tasks.get(task_id)
    if running and not running.done():
        return
    # 创建后台协程，接口函数不等待它，请求立刻返回
    task = asyncio.create_task(run_scheduled(task_id))
    _running_tasks[task_id] = task
    # 协程结束后把自己从字典里摘掉，避免字典无限增长
    task.add_done_callback(lambda _: _running_tasks.pop(task_id, None))


async def recover_transcription_tasks() -> int:
    # 上次进程退出时正在执行的任务，内存里的协程已经没了，状态却停在 PROCESSING
    # 先把它们退回 PENDING，才能被 execute_transcription 的抢占条件接住
    await TranscriptionTask.filter(status="PROCESSING").update(
        status="PENDING",
        stage="等待恢复执行",
    )
    # 连同本来就在排队的任务一起取出来重新排队
    task_ids = list(await TranscriptionTask.filter(status="PENDING").values_list("id", flat=True))
    for task_id in task_ids:
        schedule_transcription(task_id)
    if task_ids:
        logger.info("转写任务已重新入队", extra={"count": len(task_ids)})
    return len(task_ids)
