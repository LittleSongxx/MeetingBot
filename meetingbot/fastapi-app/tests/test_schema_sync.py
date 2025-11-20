"""表结构漂移检查：db/01-schema.sql 必须与 models.py 一致。

为什么需要它：表结构的真源是 `models.py`，但**空库初始化走的是 SQL 文件**（initdb 挂载），
两条路各写一份就必然漂移——加了模型字段却忘了改 SQL，只有等到"新环境起不来"才暴露。
这条检查零调用、不需要数据库，把那个窗口从"部署时"提前到"提交时"。

检查范围：**每个模型表的列名集合、每列的 NOT NULL 与否、以及列的类型口径**。
类型比对做的是"归一化到同一口径"的比较（varchar(100)/char(50) 都算字符串、
datetime 与 datetime(6) 算同一族但精确度必须一致），不做逐字节的 DDL 比对——
字符集、排序规则、索引与默认值的细节交给迁移工具（aerich）与人工评审，
硬凑一份完整类型映射表只会制造第二份会漂移的真相。
"""

from __future__ import annotations

import inspect
import re
import unittest
from pathlib import Path

import models
from tortoise import fields as tfields

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "db" / "01-schema.sql"

# CREATE TABLE `xxx` ( ... ) 里的列定义行：反引号列名后面跟类型。
_COLUMN_RE = re.compile(r"^\s*`(?P<name>[A-Za-z0-9_]+)`\s+(?P<rest>.+?),?\s*$")
# 这些行也以反引号开头，但不是列定义
_NOT_A_COLUMN = ("PRIMARY KEY", "UNIQUE INDEX", "INDEX ", "KEY ", "CONSTRAINT", "FULLTEXT")


def _declared_models() -> dict[str, type]:
    """models 模块里所有真正的表模型（排除 Tortoise 的基类 Model）。"""
    out: dict[str, type] = {}
    for obj in vars(models).values():
        if not inspect.isclass(obj) or obj.__name__ == "Model":
            continue
        meta = getattr(obj, "_meta", None)
        table = getattr(meta, "db_table", "") if meta else ""
        if table:
            out[table] = obj
    return out


def _parse_schema(path: Path) -> dict[str, dict[str, tuple[bool, str]]]:
    """从 schema.sql 解析出 {表名: {列名: (是否 NOT NULL, 类型串)}}。"""
    if not path.exists():
        raise AssertionError(f"找不到表结构文件：{path}")
    tables: dict[str, dict[str, tuple[bool, str]]] = {}
    current: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.upper().startswith("DROP TABLE"):
            continue
        m = re.match(r"^CREATE TABLE\s+`(?P<name>[A-Za-z0-9_]+)`", stripped, re.IGNORECASE)
        if m:
            current = m.group("name")
            tables[current] = {}
            continue
        if stripped.startswith(")"):  # 建表语句结束
            current = None
            continue
        if current is None or any(stripped.startswith(k) for k in _NOT_A_COLUMN):
            continue
        cm = _COLUMN_RE.match(line)
        if cm:
            col_type = re.match(r"(?P<t>[A-Za-z]+(?:\([0-9,]+\))?)", cm.group("rest").strip())
            tables[current][cm.group("name")] = (
                "NOT NULL" in cm.group("rest"),
                col_type.group("t") if col_type else "",
            )
    return tables


def _sql_type_family(sql_type: str) -> str:
    """把 SQL 类型归一到一个可比较的族。"""
    t = sql_type.lower()
    if t.startswith(("varchar", "char")):
        return "str"
    if t.startswith("bigint"):
        return "bigint"
    if t.startswith("int"):
        return "int"
    if t.startswith(("datetime", "timestamp")):
        # 精确度必须一致：datetime 与 datetime(6) 不是同一族写法，
        # 差别会在"initdb 建的库"与"迁移建的库"之间造成静默不一致
        return "datetime(6)" if "(6)" in t else "datetime"
    if t.startswith("longtext"):
        return "longtext"
    if t.startswith("text"):
        return "text"
    if t.startswith("json"):
        return "json"
    if t.startswith(("tinyint", "bool")):
        return "bool"
    if t.startswith("decimal"):
        return "decimal"
    return t


def _model_type_family(field) -> str:
    """把 Tortoise 字段归一到一个可比较的族（与 _sql_type_family 对偶）。"""
    if isinstance(field, tfields.BooleanField):
        return "bool"
    if isinstance(field, tfields.BigIntField):
        return "bigint"
    if isinstance(field, (tfields.IntField, tfields.SmallIntField)):
        return "int"
    if isinstance(field, (tfields.DatetimeField, tfields.DateField, tfields.TimeField)):
        return "datetime(6)"
    if isinstance(field, tfields.TextField):
        return "longtext"
    if isinstance(field, tfields.JSONField):
        return "json"
    if isinstance(field, tfields.CharField):
        return "str"
    if isinstance(field, tfields.DecimalField):
        return "decimal"
    return type(field).__name__


class SchemaSyncTest(unittest.TestCase):
    def setUp(self):
        self.sql = _parse_schema(SCHEMA_PATH)
        self.models = _declared_models()

    def test_all_model_tables_exist_in_schema(self):
        missing = sorted(set(self.models) - set(self.sql))
        self.assertEqual(missing, [], f"模型有表但 schema.sql 没建：{missing}")

    def test_no_orphan_tables_in_schema(self):
        orphan = sorted(set(self.sql) - set(self.models))
        self.assertEqual(orphan, [], f"schema.sql 建了但模型里没有的表：{orphan}")

    def test_columns_match_per_table(self):
        problems: list[str] = []
        for table, model in sorted(self.models.items()):
            sql_cols = set(self.sql.get(table, {}))
            model_cols = set(model._meta.db_fields)
            for col in sorted(model_cols - sql_cols):
                problems.append(f"{table}.{col}：模型里有，schema.sql 缺")
            for col in sorted(sql_cols - model_cols):
                problems.append(f"{table}.{col}：schema.sql 有，模型里没有")
        self.assertEqual(problems, [], "列不一致：\n  " + "\n  ".join(problems))

    def test_nullability_matches(self):
        problems: list[str] = []
        for table, model in sorted(self.models.items()):
            sql_cols = self.sql.get(table, {})
            for name, field in model._meta.fields_map.items():
                if name not in sql_cols:
                    continue  # 列缺失由上一个用例报，这里不重复
                model_not_null = bool(getattr(field, "pk", False)) or not bool(getattr(field, "null", False))
                if model_not_null != sql_cols[name][0]:
                    problems.append(
                        f"{table}.{name}：模型 "
                        f"{'NOT NULL' if model_not_null else 'NULL'} / schema.sql "
                        f"{'NOT NULL' if sql_cols[name][0] else 'NULL'}")
        self.assertEqual(problems, [], "可空性不一致：\n  " + "\n  ".join(problems))

    def test_column_types_match(self):
        problems: list[str] = []
        for table, model in sorted(self.models.items()):
            sql_cols = self.sql.get(table, {})
            for name, field in model._meta.fields_map.items():
                if name not in sql_cols:
                    continue
                want = _model_type_family(field)
                got = _sql_type_family(sql_cols[name][1])
                if want != got:
                    problems.append(
                        f"{table}.{name}：schema.sql 是 {sql_cols[name][1]}（族 {got}），"
                        f"ORM 期望 {want}")
        self.assertEqual(problems, [], "类型口径不一致：\n  " + "\n  ".join(problems))

    def test_timestamp_defaults_carry_matching_precision(self):
        """DATETIME(6) 配 CURRENT_TIMESTAMP 是**非法 DDL**（MySQL 1067），必须同为 (6)。

        这条不是风格检查：它是 MySQL 的硬约束，且只在真正建表时才暴露——
        实测踩过一次（把 DATETIME 对齐成 DATETIME(6) 时漏改了默认值，
        整份建表脚本在 initdb 阶段直接失败）。
        """
        problems: list[str] = []
        for line in SCHEMA_PATH.read_text(encoding="utf-8").splitlines():
            cm = _COLUMN_RE.match(line)
            if not cm:
                continue
            rest = cm.group("rest")
            if "CURRENT_TIMESTAMP" not in rest:
                continue
            has_precise_type = bool(re.match(r"datetime\(6\)", rest.strip(), re.IGNORECASE))
            has_precise_default = "CURRENT_TIMESTAMP(6)" in rest
            if has_precise_type != has_precise_default:
                problems.append(f"{cm.group('name')}：{rest.strip()[:90]}")
        self.assertEqual(problems, [], "时间默认值精度不匹配（MySQL 会以 1067 拒绝建表）：\n  "
                         + "\n  ".join(problems))


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------------------- #
# 演示数据的 INSERT 与表列数：位置化 INSERT 一旦对不上，**整份种子数据都不入库**
#
# 这组检查来自一次真实事故：给 `ai_call_log` 加了两列、改了 models.py 与 schema.sql，
# 但 `db/02-seed.sql` 里的 `INSERT INTO ai_call_log VALUES (...)` 是**位置化**的，
# 值个数没跟上。空库初始化时条 32 直接报 `Column count doesn't match value count`，
# 于是演示数据一条都没进去——而漂移测试当时只比对"模型 ↔ schema.sql"，看不见这一处。
# --------------------------------------------------------------------------- #

SEED_PATH = SCHEMA_PATH.parent / "02-seed.sql"


def _split_insert_values(statement: str) -> list[str]:
    """按顶层逗号切开 `VALUES (...)` 的值列表。

    必须正确处理单引号字符串（含 `\'` 转义）与 JSON 里成对的嵌套括号，
    否则一个含逗号的字符串就会把计数带偏。
    """
    start = statement.index("VALUES") + len("VALUES")
    body = statement[start:].strip()
    assert body.startswith("("), body[:40]
    depth = 0
    values: list[str] = []
    current: list[str] = []
    in_string = False
    escaped = False
    for char in body[1:]:
        if in_string:
            current.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == "'":
                in_string = False
            continue
        if char == "'":
            in_string = True
            current.append(char)
        elif char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            if depth == 0:
                values.append("".join(current).strip())
                return values
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            values.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    raise AssertionError("种子里有未闭合的括号")


class SeedInsertArityTest(unittest.TestCase):
    """演示数据的每个位置化 INSERT，值个数必须等于该表的列数。"""

    def _column_counts(self) -> dict[str, int]:
        schema = _parse_schema(SCHEMA_PATH)
        return {table: len(columns) for table, columns in schema.items()}

    def _inserts(self) -> list[tuple[int, str, int]]:
        counts = self._column_counts()
        rows: list[tuple[int, str, int]] = []
        text = SEED_PATH.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            match = re.match(r"INSERT INTO `([A-Za-z0-9_]+)` VALUES \(", line)
            if not match:
                continue
            table = match.group(1)
            if table not in counts:
                continue
            rows.append((number, table, len(_split_insert_values(line))))
        return rows

    def test_seed_inserts_match_the_declared_columns(self):
        expected = self._column_counts()
        offenders = [
            f"第 {number} 行 {table}: 值 {actual} 个，表 {expected[table]} 列"
            for number, table, actual in self._inserts()
            if actual != expected[table]
        ]
        self.assertEqual(offenders, [], "演示数据的位置化 INSERT 与表结构不一致：" +
                         "、".join(offenders))

    def test_seed_covers_every_table_that_has_rows(self):
        """有数据的表必须在种子里出现（防止"加了表忘了放演示数据"）。"""
        tables = {table for _, table, _ in self._inserts()}
        self.assertIn("ai_call_log", tables)
        self.assertIn("prompt_template", tables)
