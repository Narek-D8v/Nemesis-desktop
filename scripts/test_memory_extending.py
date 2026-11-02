"""Smoke-тест памяти и саморасширения (create_tool)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import get_config  # noqa: E402
from nemus.core.factory import build_core  # noqa: E402

WEATHER_CODE = '''from nemus.core.tools.base import Tool, ToolResult

class WeatherTool(Tool):
    name = "get_weather"
    description = "Демо погоды"
    parameters = {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}

    def execute(self, city="", **_):
        return ToolResult.success(f"В {city} солнечно (демо)")
'''


def main() -> int:
    core = build_core(get_config())
    print("tools:", len(core.registry))

    r = core.registry.execute("remember_fact", {"key": "имя", "value": "Иван"})
    print("remember:", r.ok, r.content)
    r = core.registry.execute("recall_fact", {"key": "имя"})
    print("recall:", r.content)

    r = core.registry.execute(
        "create_tool",
        {"name": "get_weather", "description": "погода", "code": WEATHER_CODE},
    )
    print("create_tool:", r.ok, r.content.splitlines()[0])
    r2 = core.registry.execute("get_weather", {"city": "Москва"})
    print("invoke new tool:", r2.ok, r2.content)

    # уборка
    plug = Path("plugins") / "tool_get_weather.py"
    plug.unlink(missing_ok=True)
    core.memory.set_fact("имя", "")  # noop

    assert "get_weather" in core.registry
    assert "get_weather" in core.registry.names
    print("\nMEMORY + SELF-EXTENDING SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
