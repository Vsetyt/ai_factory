"""
validators.py — Валидация Python-кода и requirements.txt.
Упрощённая версия из последнего кода восстановлена до полной с AST.
"""

import ast
import os
import tempfile
import logging
from typing import Tuple, List

logger = logging.getLogger(__name__)

DANGEROUS_CALLS = [
    (None, "eval"), (None, "exec"), (None, "compile"),
    ("os", "system"), ("os", "popen"), ("os", "execv"), ("os", "execve"),
    ("os", "remove"), ("os", "unlink"), ("shutil", "rmtree"),
]

DANGEROUS_WITH_ARGS = {
    "subprocess.run":   {"shell": True},
    "subprocess.call":  {"shell": True},
    "subprocess.Popen": {"shell": True},
}


class ASTSecurityVisitor(ast.NodeVisitor):
    def __init__(self):
        self.issues: List[str] = []
        self._in_try: bool = False

    def visit_Try(self, node):
        old = self._in_try
        self._in_try = True
        self.generic_visit(node)
        self._in_try = old

    def _get_call_name(self, node: ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            if isinstance(node.func.value, ast.Name):
                return f"{node.func.value.id}.{node.func.attr}"
        return None

    def visit_Call(self, node: ast.Call):
        name = self._get_call_name(node)
        if name:
            for module, func in DANGEROUS_CALLS:
                full = f"{module}.{func}" if module else func
                if name == full and not self._in_try:
                    self.issues.append(f"Опасный вызов {name}() в строке {node.lineno}")
            if name in DANGEROUS_WITH_ARGS:
                for kw in node.keywords:
                    danger = DANGEROUS_WITH_ARGS[name]
                    if kw.arg in danger and isinstance(kw.value, ast.Constant):
                        if kw.value.value == danger[kw.arg]:
                            self.issues.append(
                                f"Опасный аргумент {name}({kw.arg}=True) в строке {node.lineno}"
                            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        dangerous = {
            "os": {"system", "popen", "remove", "unlink", "execv", "execve"},
            "subprocess": {"call", "run", "Popen"},
            "shutil": {"rmtree"},
        }
        if node.module in dangerous:
            for alias in node.names:
                if alias.name in dangerous[node.module]:
                    self.issues.append(
                        f"Опасный импорт: from {node.module} import {alias.name} "
                        f"в строке {node.lineno}"
                    )
        self.generic_visit(node)


def _run_bandit(code: str) -> List[str]:
    tmp_path = None
    issues = []
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
            f.write(code)
            tmp_path = f.name
        from bandit.core import manager as b_manager, config as b_config
        conf = b_config.BanditConfig()
        mgr  = b_manager.BanditManager(conf, "file")
        mgr.discover_files([tmp_path], False)
        mgr.run_tests()
        for issue in mgr.get_issue_list():
            if issue.severity in ("HIGH", "MEDIUM"):
                issues.append(f"[{issue.severity}] {issue.test_id}: {issue.text} (стр.{issue.lineno})")
    except ImportError:
        logger.warning("bandit не установлен")
    except Exception as e:
        logger.error(f"bandit error: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
    return issues


def validate_code(code: str, filename: str = "code.py") -> Tuple[bool, str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"Синтаксическая ошибка: {e.msg} в строке {e.lineno}"

    visitor = ASTSecurityVisitor()
    visitor.visit(tree)
    all_issues = visitor.issues + _run_bandit(code)

    if all_issues:
        msg = " | ".join(all_issues[:5])
        if len(all_issues) > 5:
            msg += f" ... (+{len(all_issues) - 5})"
        return False, msg
    return True, "OK"


def validate_requirements(content: str) -> Tuple[bool, str]:
    for i, raw in enumerate(content.strip().split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "://" in line:
            return False, f"Строка {i}: прямая URL-зависимость запрещена: '{line}'"
        if line.startswith("-e") and "http" in line:
            return False, f"Строка {i}: -e с внешним URL запрещён: '{line}'"
        if line.startswith(("--extra-index-url", "--index-url")):
            return False, f"Строка {i}: переопределение PyPI-индекса запрещено: '{line}'"
    return True, "OK"
