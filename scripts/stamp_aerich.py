#!/usr/bin/env python
"""给“已经由 db/01-schema.sql 建好当前结构”的库登记 aerich 版本。

背景：空库走 schema.sql，不会自动创建 aerich 表；直接 `aerich upgrade` 会从 0 号
重放并在 2 号报 Duplicate column。`--fake` 能跳过执行，但 aerich 自己在写版本行时
仍需要 aerich 表存在。这个脚本先做两件事：

1. 检查库里确实已经是当前结构（缺任一哨兵列就拒绝 fake，避免把旧库伪装成新库）；
2. 幂等创建 aerich 表，再调用 `aerich upgrade --fake` 只登记版本。

用法（在 meetingbot/fastapi-app 下）：
    .venv/bin/python ../../scripts/stamp_aerich.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pymysql

APP_DIR = Path(__file__).resolve().parents[1] / "meetingbot" / "fastapi-app"

# 当前终态结构的关键哨兵：任一缺失说明这个库不是当前 schema.sql 建出来的，
# 不能 fake，必须先按真实版本做增量迁移。
REQUIRED_COLUMNS = {
    "chat_message": {"plan_json", "run_status", "thread_id", "checkpoint_id",
                     "trace_id", "charged_tokens"},
    "ai_call_log": {"cost_micro", "trace_id", "span_id"},
    "ai_model_config": {"input_price_micro_per_1k", "output_price_micro_per_1k"},
    "kb_chunk": {"embed_text"},
    "transcription_task": {"recorded_at"},
}
REQUIRED_TABLES = {"kb_index_task", "kb_document", "kb_chunk", "kb_index_outbox"}


def load_env(path: Path) -> dict[str, str]:
    """轻量读取 .env；只补进程里没有的键，不覆盖调用方显式设置的值。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def connect(env: dict[str, str]):
    return pymysql.connect(
        host=env.get("MYSQL_HOST", "127.0.0.1"),
        port=int(env.get("MYSQL_PORT", "3306")),
        user=env.get("MYSQL_USER", "root"),
        password=env.get("MYSQL_PASSWORD", ""),
        database=env.get("MYSQL_DATABASE", "meetingbot"),
        autocommit=True,
    )


def ensure_current_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME IN "
            "('kb_index_task','kb_document','kb_chunk','kb_index_outbox')")
        tables = {row[0] for row in cur.fetchall()}
        missing_tables = sorted(REQUIRED_TABLES - tables)
        missing_columns: list[str] = []
        for table, columns in REQUIRED_COLUMNS.items():
            cur.execute(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,))
            present = {row[0] for row in cur.fetchall()}
            missing_columns.extend(f"{table}.{name}" for name in sorted(columns - present))
    if missing_tables or missing_columns:
        raise SystemExit(
            "这个库不是当前 db/01-schema.sql 的终态，拒绝用 --fake 伪装成最新：\n"
            f"  缺表：{missing_tables or '无'}\n"
            f"  缺列：{missing_columns or '无'}\n"
            "请先按实际版本执行缺失的增量迁移，或重建空库。")


def ensure_aerich_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS `aerich` (
                `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
                `version` VARCHAR(255) NOT NULL,
                `app` VARCHAR(100) NOT NULL,
                `content` JSON NOT NULL
            ) CHARACTER SET utf8mb4
        """)


def main() -> int:
    env = dict(load_env(APP_DIR / ".env"))
    env.update({k: v for k, v in os.environ.items() if k in env})
    conn = connect(env)
    try:
        ensure_current_schema(conn)
        ensure_aerich_table(conn)
    finally:
        conn.close()
    proc = subprocess.run(
        [sys.executable, "-m", "aerich", "upgrade", "--fake"],
        cwd=APP_DIR, env={**os.environ, **env},
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
