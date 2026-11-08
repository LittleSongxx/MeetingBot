import unittest
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
from services.transcription_service import execute_transcription, parse_segments, prepare_audio_chunks


class TranscriptionTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await Tortoise.init(
            db_url="sqlite://:memory:",
            modules={"models": ["models"]},
            use_tz=False,
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
            "services.transcription_service.prepare_audio_chunks",
            new=AsyncMock(return_value=[source_path]),
        ), patch(
            "services.transcription_service.call_transcription_model",
            new=AsyncMock(return_value=response),
        ):
            await execute_transcription(self.task.id)

        task = await TranscriptionTask.get(id=self.task.id)
        self.assertEqual(task.status, "SUCCEEDED")
        self.assertEqual(task.progress, 100)
        self.assertEqual(task.total_duration_ms, 8500)
        segments = await TranscriptSegment.filter(task_id=task.id).order_by("segment_no")
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].speaker_label, "01-A")

        await match_speaker(
            SpeakerMatchPayload(
                task_id=task.id,
                speaker_label="01-A",
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

    def test_parse_segments_adds_chunk_prefix_and_offset(self):
        segments, duration_ms, next_no = parse_segments(
            {
                "duration": 5,
                "text": "你好",
                "segments": [
                    {"start": 1, "end": 5, "speaker": "A", "text": "你好"}
                ],
            },
            offset_ms=1200000,
            start_no=8,
            speaker_prefix="02",
        )
        self.assertEqual(segments[0]["start_ms"], 1201000)
        self.assertEqual(segments[0]["speaker_label"], "02-A")
        self.assertEqual(duration_ms, 5000)
        self.assertEqual(next_no, 9)

    def test_parse_segments_keeps_speaker_zero(self):
        """百炼的说话人编号从 0 开始，0 号说话人不能被当成缺失编号改写成 A。"""
        segments, _, _ = parse_segments(
            {
                "duration": 6,
                "segments": [
                    {"start": 0, "end": 3, "speaker": 0, "text": "先说一下目标"},
                    {"start": 3, "end": 6, "speaker": 1, "text": "我来补充"},
                    {"start": 6, "end": 6, "text": "没有编号的片段"},
                ],
            },
            offset_ms=0,
            start_no=1,
            speaker_prefix="01",
        )
        self.assertEqual([item["speaker_label"] for item in segments], ["01-0", "01-1", "01-A"])

    async def test_ffmpeg_extracts_standard_audio_chunks(self):
        if shutil.which("ffmpeg") is None:
            self.skipTest("FFmpeg is not installed")
        with tempfile.TemporaryDirectory(prefix="ai_meeting_ffmpeg_test_") as temp_dir:
            temp_path = Path(temp_dir)
            source = temp_path / "source.wav"
            output_dir = temp_path / "chunks"
            output_dir.mkdir()
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "sine=frequency=1000:duration=1",
                    str(source),
                ],
                check=True,
            )
            chunks = await prepare_audio_chunks(source, output_dir)
            self.assertEqual(len(chunks), 1)
            self.assertGreater(chunks[0].stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
