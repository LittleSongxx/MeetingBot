"""问答编排 facade。

实现已按职责拆到：
- `chat_prompts`：系统提示/引用与覆盖协议/查询包装；
- `chat_sessions`：会话 CRUD 与消息展示；
- `chat_memory`：滑窗、历史压缩、滚动摘要；
- `chat_tools`：工具装配（与 MCP 共用 kb_tools）；
- `chat_persistence`：引用解析、token 汇总、最终落库；
- `chat_turn`：一轮问答的事件流、恢复、覆盖与实体核查。

这里只做 re-export，保持原有公开 API（以及测试/接口用到的名字）不变。
"""

from __future__ import annotations

import logging

from services import (
    chat_memory,
    chat_persistence,
    chat_prompts,
    chat_sessions,
    chat_tools,
    chat_turn,
    citations,
    model_factory,
)

logger = logging.getLogger("meetingbot.chat")

ChatError = chat_sessions.ChatError
CHAT_CALL_TYPE = chat_prompts.CHAT_CALL_TYPE
SUMMARY_CALL_TYPE = chat_prompts.SUMMARY_CALL_TYPE
DEFAULT_SYSTEM_PROMPT = chat_prompts.DEFAULT_SYSTEM_PROMPT
SOURCE_COVERAGE_PROTOCOL = chat_prompts.SOURCE_COVERAGE_PROTOCOL
CITATION_PROTOCOL = chat_prompts.CITATION_PROTOCOL
SUMMARY_SYSTEM_PROMPT = chat_prompts.SUMMARY_SYSTEM_PROMPT
SUMMARY_SCHEMA = chat_prompts.SUMMARY_SCHEMA
get_model_config = chat_prompts.get_model_config
get_system_prompt = chat_prompts.get_system_prompt
question_message = chat_prompts.question_message

SESSION_TITLE_CHARS = chat_sessions.SESSION_TITLE_CHARS
MESSAGE_USER = chat_sessions.MESSAGE_USER
MESSAGE_ASSISTANT = chat_sessions.MESSAGE_ASSISTANT
create_session = chat_sessions.create_session
get_session = chat_sessions.get_session
list_sessions = chat_sessions.list_sessions
delete_session = chat_sessions.delete_session
list_messages = chat_sessions.list_messages
_message_dict = chat_sessions._message_dict

DEFAULT_HISTORY_WINDOW = chat_memory.DEFAULT_HISTORY_WINDOW
DEFAULT_SUMMARY_THRESHOLD = chat_memory.DEFAULT_SUMMARY_THRESHOLD
MIN_SUMMARY_BATCH = chat_memory.MIN_SUMMARY_BATCH
history_window = chat_memory.history_window
summary_threshold = chat_memory.summary_threshold
_positive_env = chat_memory._positive_env
build_memory = chat_memory.build_memory
_used_refs = chat_memory._used_refs
compress_answer = chat_memory.compress_answer
_heading_of = chat_memory._heading_of
_summary_transcript = chat_memory._summary_transcript
maybe_summarize = chat_memory.maybe_summarize
_EVIDENCE_HEADINGS = chat_memory._EVIDENCE_HEADINGS
_COMPRESSED_KEEP_CHARS = chat_memory._COMPRESSED_KEEP_CHARS

build_tools = chat_tools.build_tools
_tool_runner = chat_tools._tool_runner

stream_answer = chat_turn.stream_answer
resume_answer = chat_turn.resume_answer
_plan_scope = chat_turn._plan_scope
_entity_haystack = chat_turn._entity_haystack
_tool_node = chat_turn._tool_node
_stream_text = chat_turn._stream_text
_tool_arguments = chat_turn._tool_arguments
_tool_output_text = chat_turn._tool_output_text

_history_citations = chat_persistence._history_citations
_resolve_unknown_refs = chat_persistence._resolve_unknown_refs
turn_tokens = chat_persistence.turn_tokens
_persist_turn = chat_persistence._persist_turn
_fmt = chat_persistence._fmt

__all__ = [
    "ChatError", "CHAT_CALL_TYPE", "SUMMARY_CALL_TYPE", "DEFAULT_SYSTEM_PROMPT",
    "CITATION_PROTOCOL", "create_session", "get_session", "list_sessions",
    "delete_session", "list_messages", "build_memory", "maybe_summarize",
    "question_message", "build_tools", "stream_answer", "resume_answer",
    "get_model_config", "get_system_prompt", "turn_tokens", "history_window",
    "summary_threshold", "MESSAGE_USER", "MESSAGE_ASSISTANT",
    "compress_answer", "SUMMARY_SYSTEM_PROMPT",
    # facade 也暴露底层 module，供现有测试/调用方 patch（与拆分前行为一致）
    "citations", "model_factory",
]
