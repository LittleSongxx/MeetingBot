"""本地 ASR HTTP 侧车：FunASR + MOSS-Transcribe-Diarize（host GPU）。

契约（与产品 services/transcription_service.py 的 http 引擎对齐）：
    POST /transcribe  multipart 字段 file=<audio>
    200 -> {"text": str, "duration": 秒, "segments": [{"start": 秒, "end": 秒,
            "speaker": id, "text": str}], "model": str}
产品侧 TRANSCRIPTION_ENGINE=http + ASR_HTTP_URL 指向本服务即启用；
回滚 = 引擎切回 dashscope，本服务可直接停掉。

运行（独立 venv，见 requirements-moss.txt）：
    uv venv .venv-moss --python 3.12
    uv pip install --python .venv-moss/bin/python -r scripts/requirements-moss.txt
    .venv-moss/bin/python scripts/moss_asr_server.py --port 9970

注意事项（README 与部署文档的综合）：
- 模型 OpenMOSS-Team/MOSS-Transcribe-Diarize（0.9B，Apache-2.0），16kHz 输入；
- 长音频需要足够大的 max_new_tokens（默认 32768，env MOSS_MAX_NEW_TOKENS 可调）；
- 串行处理（信号量 1）：0.9B 级模型 + 长音频 KV 缓存，消费级 GPU 一次一路最稳。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

MODEL_ID = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
MAX_NEW_TOKENS = int(os.getenv("MOSS_MAX_NEW_TOKENS", "32768"))

_model = None
_model_lock = threading.Lock()  # 串行推理：长音频 + 消费级显存，一次一路


def get_model():
    global _model
    if _model is None:
        from funasr import AutoModel
        _model = AutoModel(model=MODEL_ID, device="cuda" if os.environ.get("MOSS_DEVICE", "cuda") == "cuda" else "cpu")
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


from fastapi import FastAPI, UploadFile
from fastapi.responses import JSONResponse

app = FastAPI(title="local-asr-sidecar", docs_url=None)


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_ID, "loaded": _model is not None}


@app.post("/transcribe")
async def transcribe(file: UploadFile):
    with tempfile.TemporaryDirectory(prefix="moss_asr_") as tmp:
        raw = Path(tmp) / ("input" + Path(file.filename or "x.wav").suffix)
        raw.write_bytes(await file.read())
        wav = Path(tmp) / "input.wav"
        to_wav_16k(raw, wav)
        if not wav.exists():
            wav = raw  # 宿主无 ffmpeg：原文件直送（mp3/wav 可直读）
        with _model_lock:
            started = time.monotonic()
            result = get_model().generate(
                input=str(wav), max_new_tokens=MAX_NEW_TOKENS,
                cache={}, batch_size_s=300)
        elapsed = time.monotonic() - started
    # FunASR 输出：result[0]["sentence_info"]，每段含 start/end/spk/text（秒）
    payload = result[0] if isinstance(result, list) else result
    segments = []
    texts = []
    for seg in payload.get("sentence_info") or []:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        # FunASR sentence_info 的时间戳单位是毫秒；产品契约是秒
        start = float(seg.get("start") or 0) / 1000.0
        end = float(seg.get("end") or 0) / 1000.0 / 1  # noqa: 保持显式
        end = float(seg.get("end") or 0) / 1000.0
        segments.append({"start": start, "end": end,
                         "speaker": seg.get("spk"), "text": text})
        texts.append(text)
    duration = max((s["end"] for s in segments), default=0.0)
    return JSONResponse({
        "text": "\\n".join(texts),
        "duration": duration,
        "segments": segments,
        "model": MODEL_ID,
        "elapsed_seconds": round(elapsed, 1),
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
