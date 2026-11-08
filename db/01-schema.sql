-- 原版 AI 会议纪要辅助系统：13 张表建表脚本（数据库容器启动时执行一次）
-- 来源：SourceCode/ai_meeting/ai_meeting.sql（干净初始化脚本），已去掉 CREATE DATABASE/USE。

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;


DROP TABLE IF EXISTS `agent_step`;
DROP TABLE IF EXISTS `agent_run`;
DROP TABLE IF EXISTS `meeting_minutes`;
DROP TABLE IF EXISTS `prompt_template`;
DROP TABLE IF EXISTS `ai_call_log`;
DROP TABLE IF EXISTS `transcript_segment`;
DROP TABLE IF EXISTS `transcription_task`;
DROP TABLE IF EXISTS `ai_model_config`;
DROP TABLE IF EXISTS `meeting_material`;
DROP TABLE IF EXISTS `meeting_participant`;
DROP TABLE IF EXISTS `meeting`;
DROP TABLE IF EXISTS `user`;
DROP TABLE IF EXISTS `department`;

CREATE TABLE `department` (
  -- 部门主键，员工表的 department_id 和页面部门下拉框关联此值
  `id` int NOT NULL AUTO_INCREMENT COMMENT '部门ID',
  -- 新增和编辑弹窗的部门名称，后端按名称检查重复
  `name` varchar(100) NOT NULL COMMENT '部门名称',
  -- 部门说明输入框内容，部门列表展示
  `description` varchar(500) DEFAULT NULL COMMENT '部门说明',
  -- NORMAL 部门进入员工编辑弹窗的下拉选项，DISABLED 部门不进入该选项列表
  `status` varchar(20) NOT NULL DEFAULT 'NORMAL' COMMENT '状态：NORMAL正常、DISABLED停用',
  -- 新增部门时写入，用于列表展示
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  -- 部门内容更新时刷新，用于列表展示
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_department_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='部门表';

CREATE TABLE `user` (
  -- 用户主键。登录时写进 Token 的 sub 字段，后续每个请求都靠它反查当前登录用户
  `id` int NOT NULL AUTO_INCREMENT COMMENT '用户ID',
  -- 登录账号。注册页输入框的值，登录时按它查询用户记录，全表唯一
  `username` varchar(100) NOT NULL COMMENT '登录账号',
  -- 登录密码。注册时原样保存，登录时与页面输入的密码直接比对
  `password` varchar(255) NOT NULL COMMENT '登录密码',
  -- 姓名。注册页没有姓名输入框，后端用账号填充；会议参会人、说话人匹配、纪要创建人都展示这个字段
  `name` varchar(100) NOT NULL COMMENT '姓名',
  -- 头像地址。第 3 章个人资料页上传后写入，管理端顶部和用户列表读取展示
  `avatar` varchar(500) DEFAULT NULL COMMENT '头像地址',
  -- 角色。注册接口固定写 EMPLOYEE；第 4 章的 require_admin 依赖按它判断能否访问管理员接口
  `role` varchar(30) NOT NULL DEFAULT 'EMPLOYEE' COMMENT '角色：ADMIN管理员、EMPLOYEE员工',
  -- 联系电话。第 3 章个人资料页和第 2 章员工账号管理页维护
  `phone` varchar(30) DEFAULT NULL COMMENT '联系电话',
  -- 电子邮箱。第 3 章个人资料页和第 2 章员工账号管理页维护
  `email` varchar(100) DEFAULT NULL COMMENT '电子邮箱',
  -- 所属部门ID。注册时为空，由管理员在第 2 章分配；第 2 章的部门成员页面按它取同事
  `department_id` int DEFAULT NULL COMMENT '所属部门ID',
  -- 员工职位。第 2 章由管理员维护，第 9 章的说话人建议匹配把它写进参会人员名单交给模型
  `position` varchar(100) DEFAULT NULL COMMENT '员工职位',
  -- 账号状态。登录时必须是 NORMAL；第 4 章每次解析 Token 后也会重新检查，停用后旧 Token 立即失效
  `status` varchar(20) NOT NULL DEFAULT 'NORMAL' COMMENT '账号状态：NORMAL正常、DISABLED停用',
  -- 创建时间。数据库默认写入当前时间
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  -- 更新时间。每次更新记录时由数据库自动刷新
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_user_username` (`username`),
  KEY `idx_user_role` (`role`),
  KEY `idx_user_department_id` (`department_id`),
  KEY `idx_user_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='统一用户表';

CREATE TABLE `meeting` (
  -- 会议主键。第 6 章资料、第 8 章转写、第 11 章纪要都按它关联会议
  `id` int NOT NULL AUTO_INCREMENT COMMENT '会议ID',
  -- 会议编号。由后端按 MTG + 时间 + 随机串生成，列表第一列展示
  `meeting_no` varchar(50) NOT NULL COMMENT '会议编号',
  -- 会议主题。弹窗“会议主题”输入框的值，列表查询按它模糊匹配
  `title` varchar(255) NOT NULL COMMENT '会议主题',
  -- 开始时间。弹窗“开始时间”选择器的值，列表默认按它倒序排
  `start_time` datetime NOT NULL COMMENT '开始时间',
  -- 结束时间。弹窗“结束时间”选择器的值，必须晚于开始时间
  `end_time` datetime NOT NULL COMMENT '结束时间',
  -- 会议地点。弹窗“会议地点”输入框的值，可以填会议室也可以填线上会议链接
  `location` varchar(255) DEFAULT NULL COMMENT '会议地点或线上会议地址',
  -- 会议议程。弹窗“会议议程”文本域的值，会议详情弹窗里展示
  `agenda` text COMMENT '会议议程',
  -- 创建人用户ID。取自 Token 里的当前登录用户，不接受前端传
  `creator_id` int NOT NULL COMMENT '创建人用户ID',
  -- 主持人用户ID。弹窗“主持人”下拉框的值，和创建人一起决定谁能变更会议状态
  `host_id` int NOT NULL COMMENT '主持人用户ID',
  -- 会议状态。状态机的当前位置，控制页面上哪些按钮出现
  `status` varchar(30) NOT NULL DEFAULT 'SCHEDULED' COMMENT '状态：SCHEDULED待开始、IN_PROGRESS进行中、FINISHED已结束、CANCELLED已取消',
  -- 取消原因。点击“取消”时在弹出输入框里填写，只有取消时才写入
  `cancel_reason` varchar(500) DEFAULT NULL COMMENT '取消原因',
  -- 创建时间。数据库默认写入当前时间
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  -- 更新时间。每次更新记录时由数据库自动刷新
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_meeting_no` (`meeting_no`),
  KEY `idx_meeting_creator_id` (`creator_id`),
  KEY `idx_meeting_host_id` (`host_id`),
  KEY `idx_meeting_status_start_time` (`status`, `start_time`),
  KEY `idx_meeting_start_creator_status` (`start_time`, `creator_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会议表';

CREATE TABLE `meeting_participant` (
  -- 参会记录主键
  `id` int NOT NULL AUTO_INCREMENT COMMENT '参会记录ID',
  -- 会议ID，指向 meeting.id
  `meeting_id` int NOT NULL COMMENT '会议ID',
  -- 参会员工用户ID，指向 user.id；弹窗“参会人员”多选框选中的就是它
  `user_id` int NOT NULL COMMENT '参会员工用户ID',
  -- 会议角色。由后端按创建人和主持人自动判定，不由页面直接选择
  `participant_role` varchar(30) NOT NULL DEFAULT 'PARTICIPANT' COMMENT '会议角色：CREATOR创建人、HOST主持人、PARTICIPANT参会人',
  -- 参会状态。创建人和主持人自动记为参加，其他人默认待确认
  `response_status` varchar(30) NOT NULL DEFAULT 'PENDING' COMMENT '参会状态：PENDING待确认、ACCEPTED参加、DECLINED不参加',
  -- 创建时间。数据库默认写入当前时间
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_meeting_participant` (`meeting_id`, `user_id`),
  KEY `idx_meeting_participant_user_id` (`user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会议参会人员表';

CREATE TABLE `meeting_material` (
  -- 会议资料主键。下载、删除、发起转写都按它定位
  `id` int NOT NULL AUTO_INCREMENT COMMENT '会议资料ID',
  -- 所属会议ID，指向 meeting.id。页面顶部选中的会议决定它的取值
  `meeting_id` int NOT NULL COMMENT '所属会议ID',
  -- 上传时的原始文件名。列表“文件名”列展示它，下载时也用它作为保存名
  `file_name` varchar(255) NOT NULL COMMENT '用户上传时的原始文件名',
  -- 服务端存储用的文件名，由随机串加扩展名组成，避免同名文件互相覆盖
  `storage_name` varchar(255) NOT NULL COMMENT '服务端唯一存储文件名',
  -- 资料类型。由后端按扩展名判定，决定列表标签颜色和是否显示“发起转写”按钮
  `file_type` varchar(30) NOT NULL COMMENT '资料类型：AUDIO录音、VIDEO视频、ATTACHMENT附件',
  -- 浏览器上传时带的 MIME 类型，下载时按它设置响应类型
  `content_type` varchar(150) DEFAULT NULL COMMENT '文件MIME类型',
  -- 小写扩展名。上传时按它判断资料类型，也决定资料能不能在线预览
  `file_ext` varchar(30) DEFAULT NULL COMMENT '小写文件扩展名',
  -- 文件字节数，列表“文件大小”列由它换算成 KB 或 MB
  `file_size` bigint NOT NULL DEFAULT 0 COMMENT '文件字节数',
  -- 文件内容的 SHA-256 摘要，同一会议内重复上传靠它识别
  `file_hash` char(64) NOT NULL COMMENT '文件SHA-256摘要，用于会议内重复上传判断',
  -- 上传人用户ID，取自 Token 里的当前登录用户，列表“上传人”列展示他的姓名
  `uploader_id` int NOT NULL COMMENT '上传人用户ID',
  -- 上传时间。数据库默认写入当前时间，列表按它倒序排
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '上传时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_meeting_material_storage_name` (`storage_name`),
  UNIQUE KEY `uk_meeting_material_hash` (`meeting_id`, `file_hash`),
  KEY `idx_meeting_material_type_time` (`meeting_id`, `file_type`, `create_time`),
  KEY `idx_meeting_material_uploader_id` (`uploader_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会议录音、视频和附件表';

CREATE TABLE `ai_model_config` (
  -- 模型配置主键。转写任务、会议纪要、纪要自检运行都记着它
  `id` int NOT NULL AUTO_INCREMENT COMMENT 'AI模型配置ID',
  -- 配置名称。弹窗“配置名称”输入框的值，列表第一列展示，查询按它模糊匹配
  `name` varchar(100) NOT NULL COMMENT '配置名称',
  -- 模型用途。弹窗“模型用途”下拉框的值，决定这条配置被哪个功能取用
  `model_type` varchar(30) NOT NULL DEFAULT 'TRANSCRIPTION' COMMENT 'TRANSCRIPTION、MINUTES、AGENT、SPEAKER',
  -- 服务商类型。弹窗里是只读输入框，切换用途时自动填
  -- 纪要、纪要自检和说话人匹配走 OPENAI_COMPATIBLE，转写走百炼自己的 DASHSCOPE 录音文件识别协议
  `provider` varchar(50) NOT NULL DEFAULT 'OPENAI_COMPATIBLE' COMMENT '服务商类型：OPENAI_COMPATIBLE、DASHSCOPE',
  -- API 基础地址。弹窗“API地址”输入框的值，纪要、自检、说话人匹配把它交给 ChatOpenAI，转写在它后面拼百炼的录音识别路径
  `base_url` varchar(500) NOT NULL COMMENT '模型服务API基础地址',
  -- API Key。弹窗“API Key”输入框的值，调用时放进 Authorization 请求头；返回给页面时做掩码处理
  `api_key` varchar(1000) NOT NULL COMMENT '模型服务API Key',
  -- 模型名称。弹窗“模型名称”输入框的值，作为请求体里的 model 参数
  `model_name` varchar(100) NOT NULL COMMENT '模型服务中的模型名称',
  -- 请求超时秒数。弹窗“超时秒数”数字框的值，作为 ChatOpenAI 或 httpx 客户端的超时时间
  `timeout_seconds` int NOT NULL DEFAULT 600 COMMENT '单个分段请求超时秒数',
  -- 是否启用。弹窗“启用配置”开关的值，同一 model_type 下只允许一条为 1
  `enabled` tinyint(1) NOT NULL DEFAULT 0 COMMENT '是否启用：0否、1是',
  -- 创建时间。数据库默认写入当前时间
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  -- 更新时间。每次更新记录时由数据库自动刷新
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  KEY `idx_ai_model_type_enabled` (`model_type`, `enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI模型配置表';

CREATE TABLE `transcription_task` (
  -- 转写任务主键。第 11 章生成纪要时按它取转写文本
  `id` int NOT NULL AUTO_INCREMENT COMMENT '转写任务ID',
  -- 会议ID。从资料记录带过来，列表和权限判断都用它
  `meeting_id` int NOT NULL COMMENT '会议ID',
  -- 录音或视频资料ID，指向 meeting_material.id；执行时按它找到磁盘文件
  `material_id` int NOT NULL COMMENT '录音或视频资料ID',
  -- 本次使用的AI模型配置ID，指向第 7 章的 ai_model_config.id
  `model_config_id` int NOT NULL COMMENT '本次使用的AI模型配置ID',
  -- 发起人用户ID，取自 Token 里的当前登录用户，列表“发起人”列展示他的姓名
  `initiator_id` int NOT NULL COMMENT '发起人用户ID',
  -- 音频主要语言。发起转写时在弹窗选择，作为模型请求参数
  `language` varchar(20) NOT NULL DEFAULT 'zh' COMMENT '音频主要语言ISO-639-1代码',
  -- 任务状态。决定列表标签颜色，以及“重试”“生成纪要”按钮是否出现
  `status` varchar(30) NOT NULL DEFAULT 'PENDING' COMMENT '状态：PENDING、PROCESSING、SUCCEEDED、FAILED',
  -- 当前执行阶段的中文描述，例如“正在转写第2/3个分段”，详情弹窗“进度”一栏展示
  `stage` varchar(100) NOT NULL DEFAULT '等待执行' COMMENT '当前执行阶段',
  -- 转写进度百分比，列表进度条按它渲染
  `progress` int NOT NULL DEFAULT 0 COMMENT '转写进度0至100',
  -- 已重试次数。列表“重试”列展示，达到上限后重试按钮消失
  `retry_count` int NOT NULL DEFAULT 0 COMMENT '已重试次数',
  -- 允许的最大重试次数，建表时固定为 3
  `max_retry` int NOT NULL DEFAULT 3 COMMENT '允许的最大重试次数',
  -- 音频总时长毫秒。所有分段时长累加得到，列表“时长”列由它换算成时分秒
  `total_duration_ms` bigint NOT NULL DEFAULT 0 COMMENT '音频总时长毫秒',
  -- 全部转写文本。各分段文本拼接而成，第 9 章修订片段后会重新拼接，第 14 章纪要自检审查时读取它
  `full_text` longtext COMMENT '全部转写文本',
  -- 失败原因。执行过程中抛出的异常信息，详情弹窗展示
  `error_message` text COMMENT '失败原因',
  -- 开始执行时间。任务从 PENDING 转 PROCESSING 时写入
  `started_at` datetime DEFAULT NULL COMMENT '开始执行时间',
  -- 完成或失败时间
  `finished_at` datetime DEFAULT NULL COMMENT '完成或失败时间',
  -- 创建时间。数据库默认写入当前时间，列表按它倒序排
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  -- 更新时间
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_transcription_material_id` (`material_id`),
  KEY `idx_transcription_meeting_status` (`meeting_id`, `status`),
  KEY `idx_transcription_initiator_id` (`initiator_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='音视频异步转写任务表';

CREATE TABLE `transcript_segment` (
  -- 转写片段主键
  `id` int NOT NULL AUTO_INCREMENT COMMENT '转写片段ID',
  -- 所属转写任务ID，指向 transcription_task.id
  `task_id` int NOT NULL COMMENT '转写任务ID',
  -- 会议ID。冗余一份，按会议取转写内容时不用再关联任务表
  `meeting_id` int NOT NULL COMMENT '会议ID',
  -- 时间轴顺序号。从 1 开始连续编号，详情弹窗按它升序展示
  `segment_no` int NOT NULL COMMENT '时间轴顺序号',
  -- 片段开始毫秒。已加上所在分段的时间偏移，是整段音频里的绝对位置
  `start_ms` bigint NOT NULL DEFAULT 0 COMMENT '片段开始毫秒',
  -- 片段结束毫秒
  `end_ms` bigint NOT NULL DEFAULT 0 COMMENT '片段结束毫秒',
  -- 模型识别的说话人标签，形如 01-0：前半段是分段序号，后半段是百炼返回的说话人编号，没有编号时为 A
  `speaker_label` varchar(50) DEFAULT NULL COMMENT '模型识别的说话人标签',
  -- 人工匹配的参会用户ID。第 9 章在详情弹窗里选参会人后写入
  `speaker_user_id` int DEFAULT NULL COMMENT '人工匹配的参会用户ID',
  -- 模型原始转写文本。写入后不再改动，用于和修订后的文本对照
  `original_text` text NOT NULL COMMENT '模型原始转写文本',
  -- 页面当前使用的可修订文本。第 9 章修订片段改的是它，第 11 章生成纪要读的也是它
  `content` text NOT NULL COMMENT '页面当前使用的可修订文本',
  -- 最后修订人用户ID
  `revised_by` int DEFAULT NULL COMMENT '最后修订人用户ID',
  -- 最后修订时间
  `revised_time` datetime DEFAULT NULL COMMENT '最后修订时间',
  -- 创建时间
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  -- 更新时间
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_transcript_task_segment` (`task_id`, `segment_no`),
  KEY `idx_transcript_meeting_time` (`meeting_id`, `start_ms`),
  KEY `idx_transcript_speaker_user_id` (`speaker_user_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='带时间轴和说话人的转写片段表';

CREATE TABLE `ai_call_log` (
  -- 日志主键，每次模型调用写一条
  `id` int NOT NULL AUTO_INCREMENT COMMENT 'AI调用日志ID',
  -- AIC 前缀加随机串，写日志时生成
  `request_id` varchar(50) NOT NULL COMMENT '单次模型请求唯一编号',
  -- TRANSCRIPTION 转写、MINUTES_CHUNK 纪要分段、MINUTES_MERGE 纪要合并、
  -- AGENT_REVIEW 自检审查、AGENT_REFINE 自检重写、SPEAKER_MATCH 说话人匹配
  `call_type` varchar(50) NOT NULL COMMENT '转写、纪要、纪要自检或说话人匹配的调用类型',
  -- 这次调用属于哪条业务记录：转写和说话人匹配指向转写任务，纪要两类指向纪要，自检两类指向自检运行
  -- 运行观测页判断员工能不能看这条日志时按它比对
  `biz_id` int NOT NULL COMMENT '关联转写任务、会议纪要、自检运行或说话人匹配任务ID',
  -- 调用时使用的 ai_model_config.id，运行观测页的模型下拉框按它筛选
  `model_config_id` int NOT NULL COMMENT '模型配置ID',
  -- 调用时的模型名称，模型排行表格展示它
  `model_name` varchar(100) NOT NULL COMMENT '调用的模型名称',
  -- 文本模型是系统提示词加用户提示词的 UTF-8 字节数，转写是音频分段文件的字节数
  `request_size` bigint NOT NULL DEFAULT 0 COMMENT '本次请求内容字节数',
  -- 文本模型是返回文本的 UTF-8 字节数，转写是识别结果转成字符串后的长度
  `response_size` bigint NOT NULL DEFAULT 0 COMMENT '模型返回JSON字节数',
  -- SUCCEEDED 或 FAILED，成功率和最近失败记录按它统计
  `status` varchar(30) NOT NULL COMMENT '调用状态：SUCCEEDED、FAILED',
  -- 从发出请求到拿到结果的毫秒数，平均耗时按它计算
  `elapsed_ms` bigint NOT NULL DEFAULT 0 COMMENT '调用耗时毫秒',
  -- Token 用量，Token 总量、每日趋势和模型排行按它累加
  `usage_json` json DEFAULT NULL COMMENT '模型返回的usage统计',
  -- 失败时的异常信息，最近失败记录表展示它
  `error_message` text COMMENT '调用失败原因',
  -- 调用时间，运行观测的日期范围和每日趋势按它归档
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '调用时间',
  PRIMARY KEY (`id`),
  -- 请求编号唯一
  UNIQUE KEY `uk_ai_call_request_id` (`request_id`),
  -- 按调用类型和时间查日志走这个索引
  KEY `idx_ai_call_type_time` (`call_type`, `create_time`),
  -- 按业务记录查日志走这个索引
  KEY `idx_ai_call_biz_id` (`biz_id`),
  -- 运行观测按模型、日期范围查日志走这个索引
  KEY `idx_ai_call_model_time_status` (`model_config_id`, `create_time`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI模型调用日志表';

CREATE TABLE `prompt_template` (
  -- prompt_template.id：Prompt模板ID
  `id` int NOT NULL AUTO_INCREMENT COMMENT 'Prompt模板ID',
  -- 模板唯一编码。业务代码按它取模板，例如 PromptTemplate.get(code="MINUTES_REVIEW")
  -- 页面上不展示也不允许修改，改了对应功能就取不到模板
  `code` varchar(100) NOT NULL COMMENT '模板唯一编码',
  -- 模板名称。编辑弹窗“模板名称”输入框的值，列表第一列展示，查询按它模糊匹配
  `name` varchar(100) NOT NULL COMMENT '模板名称',
  -- 应用场景。页面顶部下拉框按它筛选，编辑弹窗里只读展示
  -- MEETING_MINUTES 纪要生成、AGENT 纪要自检、TRANSCRIPTION 说话人建议匹配
  `scene_type` varchar(50) NOT NULL DEFAULT 'MEETING_MINUTES' COMMENT '使用场景',
  -- 系统指令。编辑弹窗“系统指令”文本域的值，调模型时作为 SystemMessage 系统消息发送
  `system_prompt` longtext NOT NULL COMMENT '系统指令',
  -- 用户模板。编辑弹窗“用户模板”文本域的值，里面的花括号变量由业务代码替换成真实内容
  `user_prompt` longtext NOT NULL COMMENT '包含变量占位符的用户模板',
  -- 是否启用。编辑弹窗“启用模板”开关的值，业务代码取模板时都带 enabled=True 条件
  `enabled` tinyint(1) NOT NULL DEFAULT 1 COMMENT '是否启用',
  -- prompt_template.create_time：创建时间
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  -- prompt_template.update_time：更新时间
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_prompt_code` (`code`),
  KEY `idx_prompt_scene_enabled` (`scene_type`, `enabled`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='Prompt模板表';

CREATE TABLE `meeting_minutes` (
  -- meeting_minutes.id：会议纪要ID
  `id` int NOT NULL AUTO_INCREMENT COMMENT '会议纪要ID',
  -- 属于哪场会议，列表和权限判断都按它关联到 meeting 表
  `meeting_id` int NOT NULL COMMENT '会议ID',
  -- 这份纪要是从哪个转写任务来的。第 12 章按它反查出音视频文件名显示在列表上
  `source_task_id` int NOT NULL COMMENT '来源转写任务ID',
  -- 生成这份纪要用的是哪条模型配置，换模型之后老纪要还能查到是谁生成的
  `model_config_id` int NOT NULL COMMENT '纪要生成模型配置ID',
  -- GENERATING 生成中、DRAFT 已生成待修订、CONFIRMED 已确认、FAILED 生成失败
  `status` varchar(30) NOT NULL DEFAULT 'GENERATING' COMMENT '状态：GENERATING、DRAFT、CONFIRMED、FAILED',
  -- 当前执行到哪一步的中文描述，生成过程中不断被覆盖
  `stage` varchar(100) NOT NULL DEFAULT '等待生成' COMMENT '当前生成阶段',
  -- 0 到 100 的进度，页面上的进度条读它
  `progress` int NOT NULL DEFAULT 0 COMMENT '生成进度0至100',
  -- 下面六个字段是模型的产出，第 12 章的修订和第 15 章的自检修订改的都是它们
  `summary` longtext COMMENT '会议摘要',
  -- meeting_minutes.topics_json：议题总结数组
  `topics_json` json NOT NULL COMMENT '议题总结数组',
  -- meeting_minutes.viewpoints_json：发言观点数组
  `viewpoints_json` json NOT NULL COMMENT '发言观点数组',
  -- meeting_minutes.decisions_json：会议决策数组
  `decisions_json` json NOT NULL COMMENT '会议决策数组',
  -- meeting_minutes.pending_items_json：待确认事项数组
  `pending_items_json` json NOT NULL COMMENT '待确认事项数组',
  -- meeting_minutes.risks_json：风险与争议点数组
  `risks_json` json NOT NULL COMMENT '风险与争议点数组',
  -- 谁发起的这次生成，重新生成时会被更新成新的发起人
  `created_by` int NOT NULL COMMENT '生成发起人ID',
  -- 第 12 章人工确认时写入，重新生成会把这两个字段清空
  `confirmed_by` int DEFAULT NULL COMMENT '确认人ID',
  -- meeting_minutes.confirmed_time：确认时间
  `confirmed_time` datetime DEFAULT NULL COMMENT '确认时间',
  -- 生成失败的原因，页面详情里展示
  `error_message` text COMMENT '生成失败原因',
  -- meeting_minutes.create_time：创建时间
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  -- meeting_minutes.update_time：更新时间
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  -- 一个转写任务只保留一份纪要。重新生成时覆盖原来那条，这条唯一索引兜住并发插入
  UNIQUE KEY `uk_minutes_source_task` (`source_task_id`),
  -- 列表按会议加状态筛选走这个索引
  KEY `idx_minutes_meeting_status` (`meeting_id`, `status`),
  -- 按来源转写任务反查纪要走这个索引，第 12 章的删除转写任务会用到
  KEY `idx_minutes_source_task_id` (`source_task_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='AI会议纪要表';

CREATE TABLE `agent_run` (
  -- 运行主键，agent_step.run_id 和自检两类 ai_call_log.biz_id 都指向它
  `id` int NOT NULL AUTO_INCREMENT COMMENT '自检运行ID',
  -- 运行编号在接口里用 AGR 加一段十六进制随机串生成，页面表格第一列展示的就是它
  `run_no` varchar(50) NOT NULL COMMENT '自检运行唯一编号',
  -- 被自检的是哪份纪要，修订稿最后也是覆盖回这一份
  `minutes_id` int NOT NULL COMMENT '被自检的会议纪要ID',
  -- 从纪要记录上带过来，列表页按它关联出会议主题，删除会议时也按它清理运行记录
  `meeting_id` int NOT NULL COMMENT '所属会议ID',
  -- 发起人，员工只能在列表里看到自己发起的运行
  `user_id` int NOT NULL COMMENT '发起人用户ID',
  -- 发起时从 ai_model_config 里取当前启用的 AGENT 配置，整次运行都用这一个模型
  `model_config_id` int NOT NULL COMMENT '自检模型配置ID',
  -- RUNNING 反思环执行中；WAITING_CONFIRMATION 改出了修订稿等人确认；
  -- SUCCEEDED 自检通过没改动或者修订稿已被采纳；REJECTED 人工放弃了修订；
  -- FAILED 模型调用出错；INTERRUPTED 人在页面上点了中断
  `status` varchar(30) NOT NULL DEFAULT 'RUNNING' COMMENT 'RUNNING、WAITING_CONFIRMATION、SUCCEEDED、REJECTED、FAILED、INTERRUPTED',
  -- 当前执行到哪一步的中文描述，生成过程中不断被覆盖
  `stage` varchar(100) NOT NULL DEFAULT '等待执行' COMMENT '当前执行阶段',
  -- 0 到 100 的进度，页面列表上的进度条读它
  `progress` int NOT NULL DEFAULT 0 COMMENT '自检进度百分比',
  -- 已经完成审查的轮次，每轮审查结束时写入；中断恢复时状态图从这个轮次接着跑
  `current_round` int NOT NULL DEFAULT 0 COMMENT '已完成的反思轮次',
  -- 发起时页面传入的轮数上限，接口限制在 1 到 5 之间
  `max_rounds` int NOT NULL DEFAULT 2 COMMENT '反思轮次上限，一轮等于审查一次加改一版',
  -- 最后一轮审查的判定，汇总结论按它区分“复审通过”和“到上限收尾”
  `passed` tinyint(1) NOT NULL DEFAULT 0 COMMENT '最后一轮模型是否判定通过',
  -- 最后一轮审查的得分，列表「得分」列展示它
  `score` int NOT NULL DEFAULT 0 COMMENT '最后一轮的自检得分',
  -- 最后一轮审查挑出的问题条数，列表「发现问题」列展示它
  `issue_count` int NOT NULL DEFAULT 0 COMMENT '最后一轮剩余的问题条数',
  -- 最后一轮审查的问题数组，结构是 REVIEW_SCHEMA 里的 issues
  `review_json` json NOT NULL COMMENT '最后一轮的问题清单',
  -- 重写步骤产出的整份纪要，每重写一次覆盖一次；人工确认时写回 meeting_minutes
  `revised_json` json DEFAULT NULL COMMENT '待人工确认的修订稿，为空表示纪要一个字都没改',
  -- 点击覆盖或放弃的用户，删除员工账号前会检查它
  `applied_by` int DEFAULT NULL COMMENT '确认人用户ID',
  -- 点击覆盖或放弃的时间
  `applied_time` datetime DEFAULT NULL COMMENT '确认时间',
  -- 放弃修订时输入框里填的原因
  `reject_reason` varchar(500) DEFAULT NULL COMMENT '人工放弃修订的原因',
  -- 汇总步骤拼出的一句话结论，详情弹窗「自检结论」区展示它
  `final_result` longtext COMMENT '自检结论',
  -- 运行失败时的异常信息，详情弹窗「失败原因」展示它
  `error_message` text COMMENT '运行失败原因',
  -- 发起时间，列表按它倒序，运行观测按它限定日期范围
  `started_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '开始时间',
  -- 进入 SUCCEEDED、WAITING_CONFIRMATION 或 FAILED 时写入，运行观测用它算运行耗时
  `finished_at` datetime DEFAULT NULL COMMENT '结束时间',
  -- 更新时间，每次更新记录时由数据库自动刷新
  `update_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (`id`),
  -- 运行编号唯一
  UNIQUE KEY `uk_agent_run_no` (`run_no`),
  -- 发起自检时按纪要查有没有运行中或等待确认的运行，走这个索引
  KEY `idx_agent_run_minutes_status` (`minutes_id`, `status`),
  -- 员工列表按发起人过滤、按开始时间排序，走这个索引
  KEY `idx_agent_run_user_time` (`user_id`, `started_at`),
  -- 服务启动时查 RUNNING 的运行，走这个索引
  KEY `idx_agent_run_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会议纪要自检运行表';

CREATE TABLE `agent_step` (
  -- 步骤主键，详情弹窗时间线用它做 key
  `id` int NOT NULL AUTO_INCREMENT COMMENT '自检步骤ID',
  -- 挂在哪次运行下面，删除运行时按它一起删
  `run_id` int NOT NULL COMMENT '自检运行ID',
  -- 整次运行内连续递增，从 1 开始，时间线按它排序
  `step_no` int NOT NULL COMMENT '运行内步骤号',
  -- 属于第几轮反思，同一轮的审查和重写共用一个轮次号，页面上显示成「第2轮」
  `round_no` int NOT NULL DEFAULT 0 COMMENT '所属反思轮次',
  -- REVIEW 审查、REFINE 重写、FINAL 汇总结论，前端按它翻成中文
  `step_type` varchar(30) NOT NULL COMMENT 'REVIEW、REFINE、FINAL',
  -- 这一步开始前就能写死的描述，例如「第2轮：按 4 条问题重写纪要」
  `decision_summary` text COMMENT '这一步要做什么',
  -- 这一步做完的结论文本，审查步里是模型结论加上逐条问题
  `observation_summary` text COMMENT '这一步的结论，审查步里是问题清单',
  -- 这一步的结构化产物：审查步存 passed、score 和 issues，重写步存改完的整份纪要
  -- 详情弹窗里的问题卡片就是从审查步的这个字段读出来的
  `payload_json` json DEFAULT NULL COMMENT '这一步的结构化产物，审查步是打分和问题，重写步是新纪要',
  -- 开步骤时是 RUNNING，做完写 SUCCEEDED，出错或被中断写 FAILED
  `status` varchar(30) NOT NULL DEFAULT 'RUNNING' COMMENT 'RUNNING、SUCCEEDED、FAILED',
  -- 这一步花了多少毫秒，用 perf_counter 前后相减得到，页面右上角显示它
  `elapsed_ms` bigint NOT NULL DEFAULT 0 COMMENT '步骤耗时毫秒',
  -- 步骤失败的原因：模型调用异常，或者“运行已中断，本步未完成”
  `error_message` text COMMENT '步骤失败原因',
  -- 开步骤的时间，时间线节点上显示它
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  -- 步骤结束的时间
  `finish_time` datetime DEFAULT NULL COMMENT '完成时间',
  PRIMARY KEY (`id`),
  -- 同一次运行里步骤号不能重复，这条唯一索引兜住并发写入
  UNIQUE KEY `uk_agent_run_step` (`run_id`, `step_no`),
  -- 按运行查还在 RUNNING 的步骤、把它们记成失败时走这个索引
  KEY `idx_agent_step_run_status` (`run_id`, `status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='会议纪要自检步骤表';

SET FOREIGN_KEY_CHECKS = 1;
