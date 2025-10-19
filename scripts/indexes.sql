-- AIMeeting 索引补齐（2026-09-27 审计：列表页与观测页核心过滤列全表扫描）
-- 幂等：MySQL 8 支持 CREATE INDEX IF NOT EXISTS？不支持——用存储过程式判存。
-- 直接逐条执行即可；重复执行会报 Duplicate key name，忽略即可。

-- meeting_participant：高频 filter(user_id=...)（会议/转写/纪要列表的可见性收窄）
CREATE INDEX idx_participant_user ON meeting_participant (user_id);

-- transcript_segment：按 meeting_id 计数/删除（会议级联删除、纪要入口）
CREATE INDEX idx_segment_meeting ON transcript_segment (meeting_id);

-- ai_call_log：观测页按时间范围 + 模型配置过滤
CREATE INDEX idx_call_log_time_model ON ai_call_log (create_time, model_config_id);
CREATE INDEX idx_call_log_biz ON ai_call_log (biz_id, call_type);

-- meeting：列表按创建人/主持人收窄
CREATE INDEX idx_meeting_creator ON meeting (creator_id);
CREATE INDEX idx_meeting_host ON meeting (host_id);

-- transcription_task / meeting_minutes / agent_run：可见性收窄与状态过滤
CREATE INDEX idx_task_initiator ON transcription_task (initiator_id);
CREATE INDEX idx_task_meeting ON transcription_task (meeting_id);
CREATE INDEX idx_minutes_meeting ON meeting_minutes (meeting_id);
CREATE INDEX idx_minutes_created_by ON meeting_minutes (created_by);
CREATE INDEX idx_run_minutes ON agent_run (minutes_id);
CREATE INDEX idx_run_meeting ON agent_run (meeting_id);
CREATE INDEX idx_run_user ON agent_run (user_id);

-- meeting_material：按会议列资料
CREATE INDEX idx_material_meeting ON meeting_material (meeting_id);
