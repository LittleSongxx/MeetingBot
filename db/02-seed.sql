-- AI 会议纪要助手（MeetingBot）初始化种子数据
--
-- 职责：**只插数据，不建表**——表结构在 01-schema.sql。
-- 内容：2 个部门 + 业务代码依赖的 5 条 prompt_template（后端按 code 读取）。
-- 不含用户、ai_model_config 与演示会议：管理员账号、4 条模型配置和
-- 可选演示数据由后端 bootstrap.py 在容器启动时按 .env 生成/补齐。
-- 执行：只在 MySQL 数据卷为空时由 docker-entrypoint-initdb.d 执行一次。
--
-- 客户端字符集必须显式声明：mysql 8.4 官方镜像的客户端默认 latin1，
-- 不声明会把 UTF-8 中文按 latin1 读入再编码，库里出现双重编码乱码。

SET NAMES utf8mb4;
INSERT INTO `department` (`id`, `name`, `description`, `status`) VALUES
(1, '产品研发部', '负责产品设计、研发和技术评审', 'NORMAL'),
(2, '市场运营部', '负责市场活动和运营增长', 'NORMAL');


INSERT INTO `prompt_template` (`code`, `name`, `scene_type`, `system_prompt`, `user_prompt`, `enabled`) VALUES
('MEETING_MINUTES_CHUNK', '会议纪要分段提取', 'MEETING_MINUTES', '你是企业会议纪要专家。只能根据输入转写内容提取信息，不得编造事实。', '会议主题：{meeting_title}\n会议转写片段：\n{transcript}\n请提取摘要、议题、发言观点、决策、待确认事项、风险与争议点。', 1),
('MEETING_MINUTES_MERGE', '会议纪要合并', 'MEETING_MINUTES', '你是企业会议纪要专家。将多个分段结果去重、合并，保留可追踪的事实。', '会议主题：{meeting_title}\n分段结构化结果：\n{partials}\n请生成一份完整、去重的结构化会议纪要。', 1),
('MINUTES_REVIEW', '纪要自检审查', 'AGENT', '你是企业会议纪要审核员。你的职责是对照转写原文，检查纪要记全了没有、能不能落实成具体任务。只依据给到的材料判断，不得编造事实。', '会议主题：{meeting_title}\n\n转写原文：\n{transcript}\n\n当前纪要：\n{minutes_json}\n\n纪要有六块内容：summary 会议摘要、topics 议题、viewpoints 发言观点、decisions 决策、pending_items 待确认事项、risks 风险与争议。\n\n请按两组标准逐条检查。\n第一组是完整性，对照转写原文看六块内容有没有该记没记的：摘要为空或者没概括出会议要点；议题为空，或者没覆盖原文讨论过的主要话题；发言观点为空，或者没记录原文里的关键表态；原文里达成一致的事项没有出现在决策里；原文里留待后续确认的事项没有出现在待确认事项里；原文里提到的风险或分歧没有出现在风险里。这一组的问题都用 MISSING_ITEM，整块内容为空时 index 填 -1。\n第二组是可落实性，看决策和待确认事项能不能变成任务：有没有写清负责人（MISSING_OWNER）、有没有写清完成时间（MISSING_DEADLINE）、表述是不是含糊到无法执行（VAGUE 或 NOT_ACTIONABLE）。\n另外检查有没有原文里找不到依据的内容，用 UNSUPPORTED。\n\n两组标准全部没有问题时，才把 passed 置为 true 并让 issues 为空数组。只要有任何一块该记的内容没记，就不能判通过。', 1),
('MINUTES_REFINE', '纪要自检修订', 'AGENT', '你是企业会议纪要专家。你要按审核意见修订纪要，只能依据纪要和原文里已有的事实补全改写，不得编造新的事实。', '会议主题：{meeting_title}\n\n当前纪要：\n{minutes_json}\n\n审核意见：\n{issues}\n\n请按审核意见输出修订后的完整纪要，没有被指出问题的内容保持原样。', 1),
('SPEAKER_MATCH', '说话人建议匹配', 'TRANSCRIPTION', '你是会议转写分析助手。你要根据发言内容推断每个说话人标签对应的是哪位参会人员，只能从给定的参会人员名单里选，拿不准就不要选。', '会议主题：{meeting_title}\n\n参会人员名单：\n{participants}\n\n带说话人标签的转写片段：\n{transcript}\n\n请为每个说话人标签给出最可能的参会人员、判断依据和把握程度。点名句是最有力的依据，例如某人说“请王敏说一下”，紧接着开口的那个标签多半就是王敏。没有把握时把 user_id 置为 0。', 1);

