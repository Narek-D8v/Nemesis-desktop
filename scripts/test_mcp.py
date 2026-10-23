"""Smoke-тест MCP-интеграции: поднимает локальный stdio-сервер и вызывает его.

Запуск:  python scripts/test_mcp.py
Требует установленный пакет ``mcp`` (pip install "mcp>=2.0").
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nemus.config import Config, MCPServerConfig  # noqa: E402
from nemus.core.mcp import MCP_PRESETS, MCPManager, parse_claude_config  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402

SERVER_CODE = textwrap.dedent(
    '''
    """Временный MCP-сервер для smoke-теста."""

    from mcp.server.mcpserver import MCPServer

    server = MCPServer("nemesis-test")


    @server.tool()
    def echo(text: str) -> str:
        """Возвращает переданный текст."""
        return text


    @server.tool()
    def add(a: int, b: int) -> int:
        """Складывает два числа."""
        return a + b


    if __name__ == "__main__":
        server.run("stdio")
    '''
)


def test_mcp_tab_gui() -> None:
    """GUI-вкладка MCP: карточки, тумблеры, ползунок, статусы проверки."""
    from PySide6.QtWidgets import QApplication

    from nemus.gui.dialogs import SettingsDialog

    app = QApplication.instance() or QApplication([])  # noqa: F841 — держим ссылку
    dlg = SettingsDialog(None, core=None)

    # тест независим от конфига пользователя: начинаем с пустого списка карточек
    dlg._mcp_rebuild_cards([])

    # --- виджеты на месте
    for attr in (
        "chk_mcp_enabled",
        "slider_mcp_refresh",
        "lbl_mcp_refresh_val",
        "cb_mcp_presets",
        "mcp_cards_layout",
        "mcp_cards",
        "lbl_mcp_empty",
        "btn_mcp_add",
        "btn_mcp_refresh",
        "lbl_mcp_status",
        "lbl_mcp_result",
    ):
        assert hasattr(dlg, attr), f"нет виджета {attr}"
    assert dlg.mcp_cards == [], "карточек быть не должно"
    assert not dlg.lbl_mcp_empty.isHidden(), "подсказка пустоты видна"

    # --- тумблер и ползунок: галочка включает, слайдер ходит по фиксированным шагам
    assert dlg.chk_mcp_enabled.isCheckable(), "master-контрол — тумблер"
    dlg.chk_mcp_enabled.setChecked(True)
    assert dlg.chk_mcp_enabled.isChecked()
    assert dlg._mcp_refresh_index(3600) >= 0
    assert dlg.lbl_mcp_refresh_val.text(), "период подписан рядом со слайдером"
    from nemus.gui.dialogs import _fmt_refresh

    assert _fmt_refresh(5).endswith(("с", "s")), _fmt_refresh(5)
    assert _fmt_refresh(86400).endswith(("д", "d")), _fmt_refresh(86400)

    # --- пресеты: добавление сервера с подстановкой рабочей папки
    assert dlg.cb_mcp_presets.count() == len(MCP_PRESETS)
    dlg.cb_mcp_presets.setCurrentIndex(0)  # filesystem
    dlg._mcp_add_preset()
    assert len(dlg._mcp_servers) == 1, "пресет не добавил сервера"
    assert len(dlg.mcp_cards) == 1, "карточка не создалась"
    server = dlg._mcp_servers[0]
    assert server.command == "npx" and server.args[0] == "-y"
    assert server.name, "у пресета нет названия"
    card = dlg.mcp_cards[0]
    assert card.server is server, "карточка держит ту же модель"
    assert server.name in card.lbl_name.text(), card.lbl_name.text()
    assert "npx" in card.lbl_cmd.text(), card.lbl_cmd.text()
    assert card.sw_enable.isChecked(), "включённый сервер — тумблер включён"
    assert dlg.cb_mcp_presets.currentIndex() == -1, "комбо сбрасывается после добавки"
    assert dlg.lbl_mcp_empty.isHidden(), "подсказка пустоты спрятана"

    # --- правка формы карточки → модель (многострочный env в том числе)
    card.edit_name.setText("Мой сервер")
    card.edit_cmd.setText("uvx")
    card.edit_args.setText("mcp-server-fetch")
    card.edit_env.setPlainText("KEY=1\nTOKEN=abc")
    card.sw_enable.setChecked(False)
    dlg._mcp_push()
    assert server.name == "Мой сервер"
    assert server.command == "uvx"
    assert server.env == {"KEY": "1", "TOKEN": "abc"}
    assert server.enabled is False, "тумблер пишет включённость в модель"
    assert "Мой сервер" in card.lbl_name.text(), "шапка перерисована из модели"

    # --- результат проверки: зелёная точка на карточке и строка под списком
    from nemus.gui.theme import ACCENT_DANGER, ACCENT_SUCCESS

    dlg._mcp_probe_card = card
    dlg._on_mcp_probe_done(True, "fetch_page, read_url")
    assert "Ок" in dlg.lbl_mcp_result.text(), dlg.lbl_mcp_result.text()
    assert ACCENT_SUCCESS in card.lbl_dot.styleSheet(), card.lbl_dot.styleSheet()
    assert "fetch_page" in (card.lbl_dot.toolTip() or "")

    # --- ошибка проверки: красная точка
    dlg._mcp_probe_card = card
    dlg._on_mcp_probe_done(False, "spawn ENOENT")
    assert "Ошибка" in dlg.lbl_mcp_result.text(), dlg.lbl_mcp_result.text()
    assert ACCENT_DANGER in card.lbl_dot.styleSheet(), card.lbl_dot.styleSheet()
    assert "spawn ENOENT" in (card.lbl_dot.toolTip() or "")
    assert card.state == "fail" and card.message == "spawn ENOENT"

    # --- состояние живёт на карточке и не теряется при соседних операциях
    dlg._mcp_add()
    assert len(dlg._mcp_servers) == 2 and len(dlg.mcp_cards) == 2
    assert dlg.mcp_cards[0].state == "fail", "состояние соседней карточки цело"
    assert dlg.mcp_cards[1]._form_wrap.isHidden() is False, "новая карточка раскрыта"

    # --- удаление: сначала новая (пустая) карточка, потом и пресетная
    dlg._mcp_delete(dlg.mcp_cards[1])
    assert len(dlg._mcp_servers) == 1 and len(dlg.mcp_cards) == 1
    dlg._mcp_delete(dlg.mcp_cards[0])
    assert dlg._mcp_servers == [] and dlg.mcp_cards == []
    assert not dlg.lbl_mcp_empty.isHidden(), "подсказка пустоты вернулась"

    dlg.close()
    print("ok  GUI-вкладка MCP: карточки, тумблеры, ползунок, статусы")


def test_parse_claude_config() -> None:
    """Импорт mcp_config.json (формат Claude Desktop) и коллизии неймспейсов."""
    import json as json_mod

    from nemus.core.mcp import _server_namespace

    payload = {
        "mcpServers": {
            "filesystem": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "./data"],
            },
            "broken": {"args": ["нет-команды"]},  # без command — пропускается
            "github": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_TOKEN": "abc"},
            },
        }
    }
    servers = parse_claude_config(json_mod.dumps(payload))
    assert [s.name for s in servers] == ["filesystem", "github"], servers
    assert servers[0].command == "npx" and servers[0].args[-1] == "./data"
    assert servers[1].env == {"GITHUB_TOKEN": "abc"}
    assert all(s.enabled for s in servers)

    # неймспейсы: из name, при коллизии — суффикс _2
    used: set[str] = set()
    a = _server_namespace(servers[0], 0, used)
    b = _server_namespace(servers[0], 1, used)
    assert a == "filesystem" and b == "filesystem_2", (a, b)

    # кривой JSON — исключение, а не падение GUI
    try:
        parse_claude_config("{не json")
    except (ValueError, TypeError):
        pass
    else:
        raise AssertionError("parse_claude_config должен ругаться на кривой JSON")

    print("ok  parse_claude_config + неймспейсы")


def main() -> int:
    test_parse_claude_config()
    test_mcp_tab_gui()

    try:
        import mcp  # noqa: F401
    except ImportError:
        print("[skip] пакет mcp не установлен — MCP-интеграция отключена")
        return 0

    if hasattr(mcp, "__version__") and str(mcp.__version__).startswith("1."):
        print("[skip] требуется mcp>=2.0")
        return 1

    with tempfile.TemporaryDirectory(prefix="nemus-mcp-") as tmp:
        server_path = Path(tmp) / "server.py"
        server_path.write_text(SERVER_CODE, encoding="utf-8")

        config = Config()
        config.mcp.enabled = True
        config.mcp.servers = [
            MCPServerConfig(
                name="smoke",
                command=sys.executable,
                args=[str(server_path)],
                cwd=str(Path(__file__).resolve().parent.parent),
            )
        ]

        registry = ToolRegistry()
        manager = MCPManager(config, registry)
        failures: list[str] = []
        try:
            if not manager.ensure_ready():
                print("[FAIL] не удалось подключить MCP-сервер")
                return 1

            # Имена неймспейсятся: <сервер>__<инструмент>.
            echo_name, add_name = "smoke__echo", "smoke__add"
            print("Инструменты:", ", ".join(manager.tool_names))
            if echo_name not in manager.tool_names or add_name not in manager.tool_names:
                failures.append(f"ожидались инструменты {echo_name} и {add_name}")

            checks = {
                echo_name: {"text": "привет, MCP"},
                add_name: {"a": 2, "b": 40},
            }
            for name, kwargs in checks.items():
                tool = registry.get(name)
                if tool is None:
                    failures.append(f"{name}: отсутствует в реестре")
                    print(f"[!] {name} отсутствует")
                    continue
                result = tool.execute(**kwargs)
                status = "ok" if result.ok else "FAIL"
                print(f"[{status}] {name}: {result.content[:80]}")
                if not result.ok:
                    failures.append(f"{name}: {result.content[:120]}")

            add_result = registry.get(add_name)
            if add_result is not None:
                value = add_result.execute(a=2, b=40).content.strip()
                if value != "42":
                    failures.append(f"add вернул {value!r}, ожидалось '42'")

            # Статус сервера для GUI: подключён и даёт 2 инструмента.
            connected, count = manager.server_status(config.mcp.servers[0])
            if not connected or count != 2:
                failures.append(
                    f"server_status: ожидалось (True, 2), получено ({connected}, {count})"
                )

            # Проверка перечитывания инструментов (TTL/refresh).
            manager.refresh()
            if echo_name not in manager.tool_names:
                failures.append("после refresh инструменты пропали")
            else:
                again = registry.get(echo_name)
                if again is None or not again.execute(text="ещё раз").ok:
                    failures.append("после refresh инструмент echo не работает")
                else:
                    print("[ok] refresh: инструменты перечитаны и работают")
        finally:
            manager.close()

        if failures:
            print("\nПроблемы:")
            for line in failures:
                print(" -", line)
            return 1

    print("\nMCP smoke-тест пройден.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
