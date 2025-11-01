"""ASR 首测驱动：远场音频 → **产品真实转写链路**（HTTP 上传/启动/轮询）→ CER。

口径（与 METRICS_RESEARCH §6.2 的既定方案一致）：
- 金标 = 近讲话戴 TextGrid 拼接（build_reference.py）；
- 假设 = 产品 /transcription 链路对**远场 8 通道混音为单声道 16k** 的转写输出
  （这正是产品实际交付的形态；说话人分离仅支持单声道，混音是必需而非自伤）；
- 指标 = CER 三口径（raw / no_punct / normalized，cer_tool）——首测不含
  cpCER/cpDER（需说话人对齐与 MeetEval，如实声明）。

用法（宿主执行；需产品 .env 的管理员凭据与运行中的容器）：
    set -a; . ai_meeting/fastapi-app/.env; set +a
    PYTHONPATH=evaluation python evaluation/asr/first_test/run_first_test.py \
        --meeting R8001_M8004 --base-url http://127.0.0.1:19091 \
        --container aimeeting-local-backend-1
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

from asr.cer_tool import cer  # type: ignore


class API:
    """产品 HTTP API 的最小客户端（原 run_asr_smoke 已随合成集清理删除，这里内联）。
    凭据只从环境读，不落盘、不打印。"""

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None

    def request(self, method: str, path: str, payload: dict | None = None,
                upload: Path | None = None) -> dict:
        import json
        import urllib.request
        import uuid as _uuid
        url = self.base_url + path
        headers = {}
        data = None
        if self.token:
            headers["token"] = self.token
        if upload is not None:
            boundary = _uuid.uuid4().hex
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            data = (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"file\"; filename=\"{upload.name}\"\r\n"
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode() + upload.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        elif payload is not None:
            headers["Content-Type"] = "application/json;charset=utf-8"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read())
        if str(result.get("code")) != "200":
            raise SystemExit(f"API 拒绝 {path}: {result.get('msg')}")
        return result["data"]


def read_credentials() -> tuple[str, str]:
    import os
    username = os.environ.get("APP_ADMIN_USERNAME")
    password = os.environ.get("APP_ADMIN_PASSWORD")
    if not username or not password:
        raise SystemExit("需要 APP_ADMIN_USERNAME / APP_ADMIN_PASSWORD 环境变量（先 source 产品 .env）")
    return username, password

FAR_DIR = HERE / "audio" / "Eval_Ali" / "Eval_Ali_far" / "audio_dir"
TERMINAL = {"SUCCEEDED", "FAILED"}
POLL_SECONDS = 20
MAX_WAIT_SECONDS = 45 * 60


def mixdown(meeting: str, container: str, mode: str = "avg") -> Path:
    """容器内 ffmpeg：远场多轨 → 单声道 16k mp3（产品说话人分离要求单声道）。

    mode=avg：8 通道平均下混（历史行为）；mode=best：astats 逐通道 RMS 选最响
    通道（pan 单声道）——与历史实验的通道选择同一规则（该产品开关已删），
    在上传前执行（等价于产品内部选择后再分块）。
    """
    wav = sorted(FAR_DIR.glob(f"{meeting}_MS*.wav"))
    if not wav:
        raise SystemExit(f"找不到 {meeting} 的远场音频")
    source = wav[0]
    container_src = f"/evaluation/asr/first_test/audio/Eval_Ali/Eval_Ali_far/audio_dir/{source.name}"
    if mode == "best":
        # 逐通道 RMS（容器内 astats），选最不负 dB 的通道
        proc = subprocess.run(
            ["docker", "exec", container, "ffmpeg", "-hide_banner", "-i", container_src,
             "-af", "astats=metadata=0:reset=0", "-f", "null", "-"],
            capture_output=True, text=True)
        levels = []
        for line in proc.stderr.splitlines():
            if "RMS level dB:" in line:
                try:
                    levels.append(float(line.split("RMS level dB:")[1].strip()))
                except ValueError:
                    pass
        per_channel = levels[:8]
        channel = max(range(len(per_channel)), key=lambda i: per_channel[i]) if per_channel else 0
        audio_args = ["-af", f"pan=mono|c0=c{channel}"]
        print(f"best 通道: c{channel}（RMS {per_channel[channel]:.1f} dB）")
    else:
        audio_args = ["-ac", "1"]
    out = HERE / f"hyp_{meeting}_{mode}.mp3"
    container_out = f"/evaluation/asr/first_test/{out.name}"
    proc = subprocess.run(
        ["docker", "exec", container, "ffmpeg", "-y", "-v", "error",
         "-i", container_src, *audio_args, "-ar", "16000", "-b:a", "64k", container_out],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"ffmpeg 失败：{proc.stderr[:300]}")
    if not out.exists():
        raise SystemExit("混音产物未出现在宿主挂载目录")
    return out


def transcribe_via_product(audio: Path, meeting: str, base_url: str) -> dict:
    api = API(base_url)
    username, password = read_credentials()
    account = api.request("POST", "/login", {"username": username, "password": password})
    del username, password
    api.token = account["token"]
    now = datetime.now(timezone(timedelta(hours=8)))
    mtg = api.request("POST", "/meeting/add", {
        "title": f"[ASR_EVAL] AliMeeting 远场首测 {meeting}",
        "start_time": now.isoformat(), "end_time": (now + timedelta(hours=1)).isoformat(),
        "location": "本地评测", "host_id": account["id"], "participant_ids": [],
        "agenda": "evaluation=true; alimeeting_far_field=true; cer_first_test=true",
    })
    material = api.request("POST", f"/meetingMaterial/upload/{mtg['id']}", upload=audio)
    task = api.request("POST", f"/transcription/start/{material['id']}", {"language": "zh"})
    task_id = task["id"]
    started = time.monotonic()
    while True:
        time.sleep(POLL_SECONDS)
        detail = api.request("GET", f"/transcription/selectById/{task_id}")
        status = str(detail.get("status"))
        if status in TERMINAL:
            return detail
        if time.monotonic() - started > MAX_WAIT_SECONDS:
            raise SystemExit(f"转写超时未终结（{status}）")


def hypothesis_text(task: dict) -> str:
    segments = task.get("segments")
    if isinstance(segments, list) and segments:
        rows = sorted((s for s in segments if isinstance(s, dict)),
                      key=lambda s: s.get("segment_no") or 0)
        return "".join(str(s.get("content") or "") for s in rows)
    return str(task.get("full_text") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--meeting", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:19091")
    parser.add_argument("--container", default="aimeeting-local-backend-1")
    parser.add_argument("--skip-transcribe", action="store_true",
                        help="复用已有 task 快照（调试用）")
    parser.add_argument("--mixdown", choices=("avg", "best"), default="avg",
                        help="远场下混方式：avg=平均（历史），best=RMS 最响通道")
    parser.add_argument("--reuse-audio", action="store_true",
                        help="复用已存在的混音产物，不重新 ffmpeg")
    parser.add_argument("--tag", default="",
                        help="产物后缀（区分 A/B 条件，如 c3600 / best_c1200）")
    args = parser.parse_args()

    reference_path = HERE / f"reference_{args.meeting}.txt"
    reference = reference_path.read_text(encoding="utf-8")
    tag = f".{args.tag}" if args.tag else ""

    snapshot = HERE / f"task_{args.meeting}{tag}.json"
    if args.skip_transcribe and snapshot.exists():
        task = json.loads(snapshot.read_text(encoding="utf-8"))
    else:
        expected = HERE / f"hyp_{args.meeting}_{args.mixdown}.mp3"
        if args.reuse_audio and expected.exists():
            audio = expected
        else:
            audio = mixdown(args.meeting, args.container, mode=args.mixdown)
        print(f"混音完成：{audio.name}（{audio.stat().st_size // 1048576} MB），提交产品转写…")
        task = transcribe_via_product(audio, args.meeting, args.base_url)
        snapshot.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
    if task.get("status") != "SUCCEEDED":
        raise SystemExit(f"转写未成功：{task.get('status')} {task.get('error_message')}")

    hypothesis = hypothesis_text(task)
    (HERE / f"hypothesis_{args.meeting}{tag}.txt").write_text(hypothesis, encoding="utf-8")
    result = {
        "meeting": args.meeting,
        "condition": tag or "(baseline c1200 avg)",
        "reference_chars": len(reference),
        "hypothesis_chars": len(hypothesis),
        "segments": len(task.get("segments") or []),
        "cer": cer(reference, hypothesis),
        "note": "金标=近讲话戴 TextGrid；假设=产品远场单声道链路；CER 三口径",
    }
    out = HERE / f"cer_{args.meeting}{tag}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
