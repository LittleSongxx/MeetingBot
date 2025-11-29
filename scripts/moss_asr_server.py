"""本地 ASR HTTP 侧车：FunASR + MOSS-Transcribe-Diarize（host GPU）。

契约（与产品 services/transcription_service.py 的 http 引擎对齐）：
    POST /transcribe  multipart 字段 file=<audio>；可选 max_new_tokens/max_length
    200 -> {"text": str, "duration": 秒, "segments": [{"start": 秒, "end": 秒,
            "speaker": "S01", "text": str}], "model": str,
            "audio_seconds": 秒, "elapsed_seconds": 秒, "truncated": bool}
产品侧 TRANSCRIPTION_ENGINE=http + ASR_HTTP_URL 指向本服务即启用；
回滚 = 引擎切回 dashscope，本服务可直接停掉。

运行（独立 venv，见 requirements-moss.txt）：
    scripts/.venv-moss/bin/python scripts/moss_asr_server.py --port 9970 --preload
    常驻/容器管理：scripts/moss_sidecar_service.sh、compose profile: moss

环境变量（全部可选）：
    MOSS_MODEL            本地模型目录（默认空 = 直接用 HF id，需要能访问 HF）
    MOSS_MODEL_REVISION   远程 id 时固定 revision（默认 e8681d68...；本地目录自动忽略）
    MOSS_BACKEND          hf | vllm | sglang（默认 hf）
    MOSS_DEVICE           默认 cuda:0
    MOSS_DTYPE            默认 bf16
    MOSS_ATTN             默认 sdpa
    MOSS_MAX_NEW_TOKENS   若显式设置则固定生成上限；**未设置时按音频时长自动**（推荐，产品链路
                         不传该参数，固定 5120 会在 ~6 分钟处静默截断长会议）
    MOSS_MAX_LENGTH       默认 131072（上下文上限，含音频 token）

实现要点（2026-10-23 v3）：
- funasr 1.4.16 的 MOSS 适配器只认注册名
  {"MOSS-Transcribe-Diarize", "OpenMOSS-Team/MOSS-Transcribe-Diarize"}，权重目录用
  model_path 传；该模型只发布在 HuggingFace（ModelScope 查无此仓库），必须 backend="hf"。
- 长音频单次生成覆盖整段录音，text token 消耗约 14~15/秒音频：30 分钟会议约 26000
  tokens，37 分钟约 33000；默认 5120 会在 ~6 分钟处截断。v3 默认
  max_new_tokens = int(audio_s × 16) + 6144（再按 131072 上下文扣掉音频 token 后取上限）。
- 推理放进线程池执行（run_in_threadpool）：长生成期间 uvicorn 事件循环不被阻塞；
  生成本身用 _model_lock 串行（消费级 GPU 一次一路最稳）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path

MODEL_CLASS = "OpenMOSS-Team/MOSS-Transcribe-Diarize"  # FunASR 注册名，供模型类查找
MODEL_PATH = os.getenv("MOSS_MODEL", "").strip()  # 本地目录或空（远程 id）
MODEL_REVISION = os.getenv(
    "MOSS_MODEL_REVISION", "e8681d68e7042738ffca8ac8212bc8fcb1131ab8")
BACKEND = os.getenv("MOSS_BACKEND", "hf")
DEVICE = os.getenv("MOSS_DEVICE", "cuda:0")
DTYPE = os.getenv("MOSS_DTYPE", "bf16")
ATTN = os.getenv("MOSS_ATTN", "sdpa")
# None = 自动按时长；显式值 = 固定上限（兼容旧行为/消融）
MAX_NEW_TOKENS = os.getenv("MOSS_MAX_NEW_TOKENS")
MAX_LENGTH = int(os.getenv("MOSS_MAX_LENGTH", "131072"))
# 实测 text token 率 ≈14.4/s audio，取 16 留余量；尾部标签/静音再留 6144
TOKEN_RATE = 16
TOKEN_MARGIN = 6144
# 音频侧 token 约 12.5/s（whisper 1500 帧/30s，merge 4），预留给 prompt 的上下文预算
AUDIO_TOKEN_RATE = 12.6
MIN_NEW_TOKENS = 8192

_model = None
_model_lock = threading.Lock()  # 串行推理：长音频 + 消费级显存，一次一路


def build_model_kwargs() -> dict:
    local = bool(MODEL_PATH) and Path(MODEL_PATH).exists()
    kwargs = {
        # model 是 FunASR 注册名（决定模型类）；model_path 才是权重来源
        "model": MODEL_CLASS,
        "backend": BACKEND,
        "device": DEVICE,
        "dtype": DTYPE,
        "attn_implementation": ATTN,
        "disable_update": True,
    }
    if MODEL_PATH:
        kwargs["model_path"] = MODEL_PATH
    if not local and MODEL_REVISION:
        kwargs["model_revision"] = MODEL_REVISION
    return kwargs


def get_model():
    global _model
    if _model is None:
        from funasr import AutoModel
        kwargs = build_model_kwargs()
        print(f"[moss-asr] loading model kwargs={kwargs}", flush=True)
        started = time.monotonic()
        _model = AutoModel(**kwargs)
        print(f"[moss-asr] model loaded in {time.monotonic() - started:.1f}s", flush=True)
    return _model


def to_wav_16k(source: Path, target: Path) -> None:
    """任意音视频 → 16kHz 单声道 wav（模型前端要求 16k；统一在服务侧归一）。
    宿主没有 ffmpeg 时退回直接送原始文件（FunASR/torchaudio 可直接读 mp3/wav）。"""
    if shutil.which("ffmpeg") is None:
        return  # 由调用方直接用原文件
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(source),
         "-vn", "-ac", "1", "-ar", "16000", str(target)],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 转码失败：{proc.stderr[:300]}")


def audio_seconds(path: Path) -> float:
    try:
        import soundfile as sf
        return float(sf.info(str(path)).duration)
    except Exception:
        return 0.0


def resolve_max_new_tokens(requested: int | None, seconds: float, max_length: int) -> int:
    """决定本次生成上限：请求参数 > 显式 env > 按音频时长自动；再按上下文预算封顶。"""
    if requested is not None:
        tokens = int(requested)
    elif MAX_NEW_TOKENS:
        tokens = int(MAX_NEW_TOKENS)
    else:
        tokens = max(MIN_NEW_TOKENS, int(seconds * TOKEN_RATE) + TOKEN_MARGIN)
    # 上下文上限：max_length 里要扣除音频 token 与提示词余量，避免超上下文报错
    budget = int(max_length - seconds * AUDIO_TOKEN_RATE - 1024)
    return max(MIN_NEW_TOKENS, min(tokens, budget))


def _generate_sync(wav_path: str, gen_tokens: int, gen_length: int):
    """同步生成（在线程池里跑）：串行锁 + 异常 traceback 落日志。"""
    with _model_lock:
        started = time.monotonic()
        try:
            result = get_model().generate(
                input=wav_path, max_new_tokens=gen_tokens,
                max_length=gen_length, cache={})
        except Exception:
            print("[moss-asr] generate FAILED:\n" + traceback.format_exc(), flush=True)
            raise
    return result, time.monotonic() - started


from fastapi import FastAPI, Form, UploadFile  # noqa: E402 - 刻意延迟到工具函数之后，便于本文件被当脚本通读
from fastapi.responses import JSONResponse  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402

app = FastAPI(title="local-asr-sidecar", docs_url=None)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model": MODEL_CLASS,
        "model_path": MODEL_PATH,
        "loaded": _model is not None,
        "backend": BACKEND,
        "device": DEVICE,
        "dtype": DTYPE,
        "max_new_tokens_mode": ("fixed:" + str(MAX_NEW_TOKENS)) if MAX_NEW_TOKENS else "auto_by_duration",
        "max_length": MAX_LENGTH,
    }


@app.post("/transcribe")
async def transcribe(
    file: UploadFile,
    max_new_tokens: int | None = Form(default=None),
    max_length: int | None = Form(default=None),
):
    with tempfile.TemporaryDirectory(prefix="moss_asr_") as tmp:
        # 上传件与转码目标必须不同名（wav 上传时旧版 input.wav→input.wav 会撞车）
        raw = Path(tmp) / ("source" + (Path(file.filename or "x.wav").suffix or ".bin"))
        raw.write_bytes(await file.read())
        wav = Path(tmp) / "input.wav"
        to_wav_16k(raw, wav)
        if not wav.exists():
            wav = raw  # 宿主无 ffmpeg：原文件直送（mp3/wav 可直读）
        seconds = audio_seconds(wav)
        gen_length = int(max_length or MAX_LENGTH)
        gen_tokens = resolve_max_new_tokens(max_new_tokens, seconds, gen_length)
        print(f"[moss-asr] transcribe start: {file.filename} {seconds:.1f}s "
              f"max_new_tokens={gen_tokens} (requested={max_new_tokens})", flush=True)
        # 放进线程池：生成期间事件循环不阻塞（/health 仍可响应）
        result, elapsed = await run_in_threadpool(
            _generate_sync, str(wav), gen_tokens, gen_length)
    # FunASR 输出：result[0]["sentence_info"]，每段含 start/end/spk/text（毫秒）
    payload = result[0] if isinstance(result, list) else result
    segments = []
    texts = []
    for seg in payload.get("sentence_info") or []:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        # FunASR sentence_info 的时间戳单位是毫秒；产品契约是秒
        start = float(seg.get("start") or 0) / 1000.0
        end = float(seg.get("end") or 0) / 1000.0
        # MOSS 原生说话人标签是 S01/S02 字符串；产品契约只要求可序列化
        segments.append({"start": start, "end": end,
                         "speaker": seg.get("spk"), "text": text})
        texts.append(text)
    duration = max((s["end"] for s in segments), default=0.0)
    # 截断判定：覆盖率 <99% 才算静默截断（末尾几秒静音不应误报）
    truncated = bool(seconds > 0 and duration < seconds * 0.99)
    print(f"[moss-asr] transcribe done: {seconds:.1f}s audio -> {len(segments)} segments, "
          f"last_end={duration:.1f}s, {elapsed:.1f}s wall, "
          f"coverage={duration / seconds:.2%}, truncated={truncated}", flush=True)
    return JSONResponse({
        "text": "\\n".join(texts),
        "duration": duration,
        "segments": segments,
        "model": MODEL_CLASS,
        "audio_seconds": round(seconds, 1),
        "elapsed_seconds": round(elapsed, 1),
        "max_new_tokens": gen_tokens,
        "truncated": truncated,
    })


if __name__ == "__main__":
    import uvicorn
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=int(os.getenv("MOSS_ASR_PORT", "9970")))
    parser.add_argument("--preload", action="store_true", help="启动即加载模型（默认首请求加载）")
    args = parser.parse_args()
    if args.preload:
        get_model()
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="info")
