"""Простые безопасные инструменты (время, калькулятор)."""

from __future__ import annotations

import ast
import operator
from datetime import datetime

from nemus.core.tools.base import RiskLevel, Tool, ToolResult

_ALLOWED_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


class CurrentTimeTool(Tool):
    name = "get_time"
    description = "Возвращает текущую дату и время на компьютере пользователя."
    parameters = {
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "description": "Формат strftime, по умолчанию '%d.%m.%Y %H:%M:%S'.",
            }
        },
    }
    risk = RiskLevel.SAFE

    def execute(self, format: str = "%d.%m.%Y %H:%M:%S", **_: object) -> ToolResult:
        try:
            value = datetime.now().strftime(format)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Неверный формат: {exc}")
        return ToolResult.success(value)


class CalculatorTool(Tool):
    name = "calculate"
    description = (
        "Вычисляет арифметическое выражение. Поддерживает +, -, *, /, //, %, ** "
        "и скобки. Пример: '2*(3+4)'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "expression": {
                "type": "string",
                "description": "Арифметическое выражение для вычисления.",
            }
        },
        "required": ["expression"],
    }
    risk = RiskLevel.SAFE

    def execute(self, expression: str = "", **_: object) -> ToolResult:
        if not expression.strip():
            return ToolResult.failure("Пустое выражение.")
        try:
            result = self._eval(ast.parse(expression, mode="eval").body)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.failure(f"Не удалось вычислить: {exc}")
        return ToolResult.success(f"{expression} = {result}")

    def _eval(self, node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_OPS:
            left = self._eval(node.left)
            right = self._eval(node.right)
            return _ALLOWED_OPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_OPS:
            return _ALLOWED_OPS[type(node.op)](self._eval(node.operand))
        raise ValueError("Недопустимое выражение.")


def basic_tools() -> list[Tool]:
    return [CurrentTimeTool(), CalculatorTool()]
