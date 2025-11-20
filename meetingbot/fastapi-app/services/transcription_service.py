import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx

from common.times import now
from api.meeting_material import MATERIAL_DIR
from models import (
    AiCallLog,
    AiModelConfig,
    MeetingMaterial,
    TranscriptSegment,
    TranscriptionTask,
)



# ---------------------------------------------------------------------------
# 转写引擎选择（`TRANSCRIPTION_ENGINE`，默认 dashscope）：
# - dashscope：上传 OSS → 提交异步任务 → 轮询（云端商用 API）
# - http：POST 音频到本地 ASR HTTP 服务（契约：multipart 上传，返回
#   {"text": str, "duration": 秒, "segments": [{"start": 秒, "end": 秒,
#    "speaker": id, "text": str}]}——与 convert_dashscope_result 同构，
#   下游 parse_segments/纪要/自检全部无感知）。参考实现 scripts/moss_asr_server.py
#   （FunASR + MOSS-Transcribe-Diarize，host GPU 侧车）。切换与回滚只动环境变量。
# ---------------------------------------------------------------------------
TRANSCRIPTION_ENGINE_ENV = "TRANSCRIPTION_ENGINE"
ASR_HTTP_URL_ENV = "ASR_HTTP_URL"
# 本地引擎在调用日志里的模型名（如实标注来源，不与云端配置混淆）
LOCAL_ASR_MODEL_NAME = "moss-transcribe-diarize-0.9b(local)"

# 单请求直传的时长上限（秒）。云端说话人分离官方建议单次 ≤2 小时（单声道），
# 超限音频在任务开始前被 ffprobe 预检拒绝、提示拆分。不再按时间切块：
# 跨块说话人编号重置正是历史上归属损失的主因（LEDGER 轴1，切块时代 Δcp 曾达 +58pp，
# 放宽到 1 小时后仍有 +5pp；单请求直传从根上消除重置）。
MAX_DURATION_SECONDS = max(600, int(os.getenv("TRANSCRIPTION_MAX_DURATION_SECONDS") or "7200"))


def _active_engine() -> str:
    return (os.getenv(TRANSCRIPTION_ENGINE_ENV) or "dashscope").strip().lower()


def _audio_duration_seconds(source_path: Path) -> float | None:
    """ffprobe 读容器时长（秒）；读不到返回 None——探针失败不阻断任务（fail-open）。"""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(source_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if probe.returncode != 0:
        return None
    try:
        return float(probe.stdout.decode().strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def reject_overlong_audio(source_path: Path) -> None:
    """时长超过单次转写上限时抛错。任务路径与发起接口都会调用。"""
    duration = _audio_duration_seconds(source_path)
    if duration is not None and duration > MAX_DURATION_SECONDS:
        raise RuntimeError(
            f"音频时长 {int(duration // 60)} 分钟，超过单次转写上限 "
            f"{MAX_DURATION_SECONDS // 3600} 小时（说话人分离在超长音频上不可靠）。"
            "请将录音拆分为多段后分别上传。")



async def prepare_audio_file(source_path: Path, output_dir: Path) -> Path:
    """整段音频转成 ASR 需要的单声道 16kHz MP3，写入临时目录后返回文件路径。

    不再按时间切块：云端说话人分离官方支持单次 ≤2 小时（超限由预检拒绝），
    而跨块说话人编号重置是历史归属损失的主因（LEDGER 轴1）。
    """
    # 没装 FFmpeg 时直接给出明确提示，而不是让 ffmpeg 命令报找不到
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("未找到FFmpeg，请先安装并加入PATH")
    output_path = output_dir / "audio.mp3"
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
        # 多通道输入平均下混为单声道（说话人分离仅支持单声道）
        "-ac",
        "1",
        # 16kHz 采样率
        "-ar",
        "16000",
        # 64kbps 码率
        "-b:a",
        "64k",
        str(output_path),
    ]
    # FFmpeg 放到线程里同步执行。asyncio 的子进程接口在 Windows 上依赖 Proactor 事件循环，
    # 而 uvicorn 启动时会把事件循环换成 Selector 版，直接调用会抛出没有任何说明的 NotImplementedError。
    completed = await asyncio.to_thread(
        subprocess.run,
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0 or not output_path.is_file():
        message = completed.stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(message or "FFmpeg音轨提取失败")
    return output_path


async def create_call_log(
    task: TranscriptionTask,
    config: AiModelConfig,
    request_size: int,
    response_size: int,
    status: str,
    elapsed_ms: int,
    usage: dict | None = None,
    error_message: str | None = None,
    model_name: str | None = None,
):
    # 每个转写任务的一次引擎调用对应 ai_call_log 表的一行
    await AiCallLog.create(
        # 调用唯一编号，AIC 前缀加 32 位十六进制随机串
        request_id="AIC" + uuid4().hex.upper(),
        # 固定为 TRANSCRIPTION，第 17 章按它区分调用类型
        call_type="TRANSCRIPTION",
        # 业务ID是转写任务ID
        biz_id=task.id,
        # 模型配置ID和模型名，第 17 章的模型排行按它们分组显示
        model_config_id=config.id,
        model_name=model_name or config.model_name,
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


# 远端任务的状态轮询间隔和次数上限：单请求最长 2 小时音频，预算放宽到 1 小时轮询窗口
POLL_INTERVAL_SECONDS = 5
POLL_MAX_TIMES = 720


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
    # 本地引擎的日志模型名如实标注；云端引擎沿用配置里的模型名
    log_model_name = config.model_name
    try:
        if _active_engine() == "http":
            # 本地 HTTP ASR 服务：一次请求同步返回（无上传/轮询）
            endpoint = (os.getenv(ASR_HTTP_URL_ENV) or "").strip()
            if not endpoint:
                raise RuntimeError("TRANSCRIPTION_ENGINE=http 需要设置 ASR_HTTP_URL")
            log_model_name = LOCAL_ASR_MODEL_NAME
            async with httpx.AsyncClient(timeout=config.timeout_seconds) as client:
                with audio_path.open("rb") as audio_file:
                    response = await client.post(
                        endpoint, files={"file": (audio_path.name, audio_file, "audio/mpeg")})
            if response.status_code >= 400:
                raise RuntimeError(f"本地ASR服务请求失败：{response.text[:500]}")
            detail = response.json()
            response_size = len(str(detail))
            result = {"text": str(detail.get("text") or ""),
                      "duration": float(detail.get("duration") or 0),
                      "segments": [
                          {"start": float(seg.get("start") or 0),
                           "end": float(seg.get("end") or seg.get("start") or 0),
                           "speaker": seg.get("speaker"),
                           "text": str(seg.get("text") or "")}
                          for seg in (detail.get("segments") or []) if str(seg.get("text") or "").strip()]}
        else:
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
            usage=detail.get("properties") if _active_engine() != "http" else {"engine": "local-http"},
            model_name=log_model_name,
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
            model_name=log_model_name,
        )
        raise


def parse_segments(result: dict) -> tuple[list[dict], int]:
    """引擎结果转内部片段结构。单请求直传后没有偏移与前缀：时间是绝对值，
    说话人标签是引擎输出的原始编号（同一任务内全局一致）。"""
    response_segments = result.get("segments") or []
    parsed = []
    max_end_ms = 0
    for item in response_segments:
        content = str(item.get("text") or "").strip()
        # 跳过空白片段，不占用序号
        if not content:
            continue
        # 模型返回的是秒，乘 1000 转成毫秒
        start_ms = int(float(item.get("start") or 0) * 1000)
        # 没有结束时同时退回用开始时间，保证字段有值
        end_ms = int(float(item.get("end") or item.get("start") or 0) * 1000)
        max_end_ms = max(max_end_ms, end_ms)
        # 说话人编号从 0 开始，0 是有效编号，只有字段缺失时才用 A 代替
        speaker = item.get("speaker")
        parsed.append(
            {
                "segment_no": len(parsed) + 1,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "speaker_label": str(speaker) if speaker is not None else "A",
                # 原始文本和可修订文本先写成一样，第 9 章修订时只改 content
                "original_text": content,
                "content": content,
            }
        )
    # 优先用模型返回的整段时长
    duration_ms = int(float(result.get("duration") or 0) * 1000)
    # 模型没给时长或给得偏小时，用片段里最大的结束时间兜底
    duration_ms = max(duration_ms, max_end_ms)
    # 模型只给了整段文本没给分段时，把整段文本当作一个片段
    if not parsed and str(result.get("text") or "").strip():
        content = str(result["text"]).strip()
        parsed.append(
            {
                "segment_no": 1,
                "start_ms": 0,
                "end_ms": duration_ms,
                "speaker_label": "A",
                "original_text": content,
                "content": content,
            }
        )
    return parsed, duration_ms


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
        # 时长预检：超限音频在付费调用前拒绝（重试与恢复路径自动复用同一守卫）
        reject_overlong_audio(source_path)
        # 临时目录存放转换后的音频，with 结束时自动整个删掉
        with tempfile.TemporaryDirectory(prefix="ai_meeting_transcribe_") as temp_dir:
            audio_path = await prepare_audio_file(source_path, Path(temp_dir))
            # 音频准备完成，进度推到 20
            await TranscriptionTask.filter(id=task.id).update(
                stage="正在转写",
                progress=20,
            )
            # 单请求直传：整段音频一次引擎调用，说话人编号天然全局一致
            result = await call_transcription_model(task, config, audio_path)
            all_segments, duration_ms = parse_segments(result)
            full_text = str(result.get("text") or "").strip()
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
            await TranscriptionTask.filter(id=task.id).update(
                status="SUCCEEDED",
                stage="转写完成",
                progress=100,
                # 模型返回的整段时长（片段最大结束时间兜底）
                total_duration_ms=duration_ms,
                # 整体文本供第 14 章纪要自检审查读取
                full_text=full_text,
                error_message=None,
                finished_at=now(),
            )
    except Exception as error:
        # 任何一步失败都把任务标成 FAILED 并记下原因，页面上出现“重试”按钮
        await TranscriptionTask.filter(id=task.id).update(
            status="FAILED",
            stage="转写失败",
            error_message=str(error)[:2000],
            finished_at=now(),
        )


def schedule_transcription(task_id: int):
    """把转写任务交给 Temporal 执行（同步启动，不等待完成）。"""
    from services import task_queue
    asyncio.get_running_loop().create_task(
        task_queue.enqueue(task_queue.KIND_TRANSCRIPTION, task_id, (task_id,)))


def cancel_transcription(task_id: int):
    """请求取消转写 workflow；任务状态由调用方（forceFail）先写好。"""
    from services import task_queue
    asyncio.get_running_loop().create_task(
        task_queue.cancel(task_queue.KIND_TRANSCRIPTION, task_id))


async def recover_transcription_tasks():
    """兜底扫尾：重新入队"建了行但 workflow 没启动"的任务。

    正常的崩溃恢复由 Temporal 原生完成（workflow 自动续跑）；这里只覆盖
    API 写行与 start_workflow 之间的窄窗口——行是非终态而队列里没有它。
    """
    from services import task_queue
    ids = await TranscriptionTask.filter(status__in=["PENDING", "PROCESSING"]).values_list("id", flat=True)
    for task_id in ids:
        await task_queue.enqueue(task_queue.KIND_TRANSCRIPTION, task_id, (task_id,))
