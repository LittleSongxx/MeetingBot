from types import SimpleNamespace
import types
import unittest
import unittest.mock
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tortoise import Tortoise

from api.meeting_material import MATERIAL_DIR
from api.transcription import (
    SegmentPayload,
    SpeakerMatchPayload,
    StartPayload,
    get_accessible_task,
    match_speaker,
    retry,
    start,
    update_segment,
)
from common.exception_handler import CustomException
from models import (
    AiModelConfig,
    Meeting,
    MeetingMaterial,
    MeetingParticipant,
    TranscriptSegment,
    TranscriptionTask,
    User,
)
from services.transcription_service import execute_transcription, parse_segments, prepare_audio_file


class TranscriptionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=True,
            timezone="Asia/Shanghai",
        )
        await Tortoise.generate_schemas()
        self.creator = await User.create(
            username="creator_t", password="123456", name="创建人", role="EMPLOYEE"
        )
        self.participant = await User.create(
            username="participant_t", password="123456", name="参会人", role="EMPLOYEE"
        )
        self.outsider = await User.create(
            username="outsider_t", password="123456", name="无关员工", role="EMPLOYEE"
        )
        self.meeting = await Meeting.create(
            meeting_no="MTG-TRANSCRIPTION-001",
            title="转写业务验证",
            start_time="2026-08-27 09:00:00",
            end_time="2026-08-27 10:00:00",
            creator_id=self.creator.id,
            host_id=self.creator.id,
        )
        await MeetingParticipant.create(
            meeting_id=self.meeting.id,
            user_id=self.creator.id,
            participant_role="HOST",
        )
        await MeetingParticipant.create(
            meeting_id=self.meeting.id,
            user_id=self.participant.id,
            participant_role="PARTICIPANT",
        )
        self.config = await AiModelConfig.create(
            name="转写模型",
            base_url="https://api.openai.com/v1",
            api_key="unit-test-key",
            model_name="gpt-4o-transcribe-diarize",
            enabled=True,
        )
        MATERIAL_DIR.mkdir(parents=True, exist_ok=True)
        self.storage_name = "transcription-test-audio.mp3"
        (MATERIAL_DIR / self.storage_name).write_bytes(b"audio")
        self.material = await MeetingMaterial.create(
            meeting_id=self.meeting.id,
            file_name="meeting.mp3",
            storage_name=self.storage_name,
            file_type="AUDIO",
            content_type="audio/mpeg",
            file_ext="mp3",
            file_size=5,
            file_hash="a" * 64,
            uploader_id=self.creator.id,
        )
        self.task = await TranscriptionTask.create(
            meeting_id=self.meeting.id,
            material_id=self.material.id,
            model_config_id=self.config.id,
            initiator_id=self.creator.id,
            status="PENDING",
        )

    async def asyncTearDown(self):
        (MATERIAL_DIR / self.storage_name).unlink(missing_ok=True)
        await Tortoise.close_connections()

    async def test_async_execution_timeline_revision_and_speaker_match(self):
        response = {
            "text": "确认会议目标。 开始执行任务。",
            "duration": 8.5,
            "segments": [
                {"start": 0, "end": 3.5, "speaker": "A", "text": "确认会议目标。"},
                {"start": 3.5, "end": 8.5, "speaker": "B", "text": "开始执行任务。"},
            ],
        }
        source_path = MATERIAL_DIR / self.storage_name
        with patch(
            "services.transcription_service.prepare_audio_file",
            new=AsyncMock(return_value=source_path),
        ), patch(
            "services.transcription_service.call_transcription_model",
            new=AsyncMock(return_value=response),
        ), patch(
            "services.transcription_service.reject_overlong_audio",
        ):
            await execute_transcription(self.task.id)

        task = await TranscriptionTask.get(id=self.task.id)
        self.assertEqual(task.status, "SUCCEEDED")
        self.assertEqual(task.progress, 100)
        self.assertEqual(task.total_duration_ms, 8500)
        segments = await TranscriptSegment.filter(task_id=self.task.id).order_by("segment_no")
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].speaker_label, "A")

        await match_speaker(
            SpeakerMatchPayload(
                task_id=self.task.id,
                speaker_label="A",
                user_id=self.creator.id,
            ),
            self.participant,
        )
        first = await TranscriptSegment.get(id=segments[0].id)
        self.assertEqual(first.speaker_user_id, self.creator.id)

        await update_segment(
            SegmentPayload(id=first.id, content="确认本次会议目标。"),
            self.participant,
        )
        first = await TranscriptSegment.get(id=first.id)
        task = await TranscriptionTask.get(id=task.id)
        self.assertEqual(first.original_text, "确认会议目标。")
        self.assertEqual(first.content, "确认本次会议目标。")
        self.assertIn("确认本次会议目标。", task.full_text)

    async def test_outsider_cannot_read_task(self):
        with self.assertRaises(CustomException) as context:
            await get_accessible_task(self.task.id, self.outsider)
        self.assertEqual(context.exception.code, "403")

    async def test_start_and_retry_state_flow(self):
        await self.task.delete()
        with patch("api.transcription.schedule_transcription") as schedule:
            result = await start(
                self.material.id,
                StartPayload(language="zh"),
                self.participant,
            )
            task_id = result.data["id"]
            schedule.assert_called_once_with(task_id)
        task = await TranscriptionTask.get(id=task_id)
        await TranscriptionTask.filter(id=task.id).update(
            status="FAILED",
            stage="转写失败",
            error_message="network error",
        )
        with patch("api.transcription.schedule_transcription") as schedule:
            await retry(task.id, self.participant)
            schedule.assert_called_once_with(task.id)
        task = await TranscriptionTask.get(id=task.id)
        self.assertEqual(task.status, "PENDING")
        self.assertEqual(task.retry_count, 1)
        self.assertIsNone(task.error_message)

    def test_parse_segments_uses_absolute_time_and_raw_labels(self):
        segments, duration_ms = parse_segments(
            {
                "duration": 5,
                "text": "你好",
                "segments": [
                    {"start": 1, "end": 5, "speaker": "A", "text": "你好"}
                ],
            },
        )
        self.assertEqual(segments[0]["start_ms"], 1000)
        self.assertEqual(segments[0]["speaker_label"], "A")
        self.assertEqual(duration_ms, 5000)

    def test_parse_segments_keeps_speaker_zero(self):
        """百炼的说话人编号从 0 开始，0 号说话人不能被当成缺失编号改写成 A。"""
        segments, _ = parse_segments(
            {
                "duration": 6,
                "segments": [
                    {"start": 0, "end": 3, "speaker": 0, "text": "先说一下目标"},
                    {"start": 3, "end": 6, "speaker": 1, "text": "我来补充"},
                    {"start": 6, "end": 6, "text": "没有编号的片段"},
                ],
            },
        )
        self.assertEqual([item["speaker_label"] for item in segments], ["0", "1", "A"])

    async def test_ffmpeg_extracts_single_audio_file(self):
        if shutil.which("ffmpeg") is None:
            self.skipTest("FFmpeg is not installed")
        with tempfile.TemporaryDirectory(prefix="ai_meeting_ffmpeg_test_") as temp_dir:
            temp_path = Path(temp_dir)
            source = temp_path / "source.wav"
            output_dir = temp_path / "out"
            output_dir.mkdir()
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
                    str(source),
                ],
                check=True,
            )
            audio = await prepare_audio_file(source, output_dir)
            self.assertTrue(audio.is_file())
            self.assertGreater(audio.stat().st_size, 0)


class DurationPrecheckTest(unittest.TestCase):
    """时长预检：>2h 拒绝、探针失败 fail-open、env 上限可调。"""

    def test_overlong_audio_is_rejected_with_readable_message(self):
        import services.transcription_service as svc
        with unittest.mock.patch.object(
                svc.subprocess, "run",
                return_value=SimpleNamespace(returncode=0, stdout=b"9000.0\n", stderr=b"")):
            with self.assertRaises(RuntimeError) as caught:
                svc.reject_overlong_audio(Path("x.mp3"))
        self.assertIn("拆分", str(caught.exception))

    def test_probe_failure_fails_open(self):
        import services.transcription_service as svc
        with unittest.mock.patch.object(
                svc.subprocess, "run",
                return_value=SimpleNamespace(returncode=1, stdout=b"", stderr=b"err")):
            # 不抛错：探针失败不阻断任务，任务内守卫与云端限制兜底
            svc.reject_overlong_audio(Path("x.mp3"))

    def test_within_limit_passes(self):
        import services.transcription_service as svc
        with unittest.mock.patch.object(
                svc.subprocess, "run",
                return_value=SimpleNamespace(returncode=0, stdout=b"3600.0\n", stderr=b"")):
            svc.reject_overlong_audio(Path("x.mp3"))


if __name__ == "__main__":
    unittest.main()



class HttpEngineTest(unittest.IsolatedAsyncioTestCase):
    """TRANSCRIPTION_ENGINE=http 的适配分支：契约转换、引擎选择、日志模型名。"""

    async def test_active_engine_defaults_to_dashscope(self):
        from services import transcription_service as svc
        import os
        with unittest.mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("TRANSCRIPTION_ENGINE", None)
            self.assertEqual(svc._active_engine(), "dashscope")

    async def test_http_engine_converts_contract_and_logs_local_model(self):
        from services import transcription_service as svc
        contract = {"text": "甲说了。", "duration": 12.5,
                    "segments": [{"start": 0.0, "end": 5.0, "speaker": 0, "text": "甲说了。"},
                                 {"start": 5.0, "end": 12.5, "speaker": 1, "text": "乙答了。"},
                                 {"start": 13.0, "end": 14.0, "speaker": 0, "text": "  "}]}

        class FakeResponse:
            status_code = 200
            def json(self):
                return contract

        class FakeClient:
            def __init__(self, post):
                self._post = post
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
            async def post(self, *args, **kwargs):
                return self._post(*args, **kwargs)

        def fake_post(url, files=None):
            fake_post.calls.append(url)
            return FakeResponse()
        fake_post.calls = []

        logs = []
        task = types.SimpleNamespace(id=1)
        config = types.SimpleNamespace(model_name="qwen-audio-x", api_key="k",
                                       base_url="http://ds", timeout_seconds=30,
                                       model_type="TRANSCRIPTION", id=7)
        audio = Path(tempfile.mkstemp(suffix=".mp3")[1])
        audio.write_bytes(b"fake-audio")
        try:
            with unittest.mock.patch.dict("os.environ", {
                    "TRANSCRIPTION_ENGINE": "http",
                    "ASR_HTTP_URL": "http://127.0.0.1:9970/transcribe"}):
                with unittest.mock.patch.object(svc.httpx, "AsyncClient",
                                                lambda timeout: FakeClient(fake_post)):
                    with unittest.mock.patch.object(
                            svc, "create_call_log",
                            AsyncMock(side_effect=lambda *a, **k: logs.append((a, k)))):
                        result = await svc.call_transcription_model(task, config, audio)
        finally:
            audio.unlink(missing_ok=True)

        self.assertEqual(len(result["segments"]), 2)
        self.assertEqual(result["segments"][0]["speaker"], 0)
        self.assertEqual(result["duration"], 12.5)
        self.assertTrue(fake_post.calls[0].endswith("/transcribe"))
        self.assertEqual(logs[0][1].get("model_name"), svc.LOCAL_ASR_MODEL_NAME)

    async def test_http_engine_requires_url(self):
        from services import transcription_service as svc
        task = types.SimpleNamespace(id=1)
        config = types.SimpleNamespace(model_name="m", api_key="k", base_url="http://x",
                                       timeout_seconds=5, model_type="TRANSCRIPTION", id=1)
        audio = Path(tempfile.mkstemp(suffix=".mp3")[1])
        audio.write_bytes(b"x")
        try:
            with unittest.mock.patch.dict("os.environ", {
                    "TRANSCRIPTION_ENGINE": "http", "ASR_HTTP_URL": ""}):
                with unittest.mock.patch.object(svc, "create_call_log", AsyncMock()):
                    with self.assertRaises(RuntimeError):
                        await svc.call_transcription_model(task, config, audio)
        finally:
            audio.unlink(missing_ok=True)

