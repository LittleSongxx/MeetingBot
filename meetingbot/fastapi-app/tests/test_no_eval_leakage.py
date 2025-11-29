"""防止评测集过拟合的静态守卫（零调用）。

只允许**通用机制**：产品代码（services/api/common）的行为字符串里不得出现
评测集/评测文件专属标识（case id、gold 路径、某次实验名）。注释/docstring 可以引用
真实事故作为出处（不进运行时行为），因此本测试只扫**非 docstring 的字符串常量**。

这是"通用泛化"纪律的一部分：机制只能依赖 (问题, 工具 schema, 转写, 上下文) 这类
通用输入，不得按评测 case 打补丁。
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("services", "api", "common")
# 评测集/实验专属标识（产品运行时字符串里不应出现）
FORBIDDEN = re.compile(
    r"cross_?30|vcsum_?\d|extended_?68|eval_?100|trajectory_?27|claim_anchor|gold/|"
    r"qa_seed|eval100_scope|manifests?_?test|m0[1-3]\d\b")


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _runtime_strings(path: Path):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    docstrings = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            yield node.lineno, node.value


class NoEvaluationLeakageTest(unittest.TestCase):
    def test_product_runtime_strings_have_no_eval_set_identifiers(self):
        findings = []
        for directory in SCAN_DIRS:
            for path in sorted((APP / directory).rglob("*.py")):
                for line, text in _runtime_strings(path):
                    match = FORBIDDEN.search(text)
                    if match:
                        findings.append(f"{path.relative_to(APP)}:{line}: {match.group(0)}")
        self.assertEqual(findings, [],
                         "产品运行时字符串引用了评测集专属标识；请改用通用机制：\n"
                         + "\n".join(findings))


if __name__ == "__main__":
    unittest.main()
