"""Проверка раскладки вкладки MCP: карточки влезают, форма раскрывается, слайдер цел.

Запуск:  python scripts/test_mcp_layout.py
(Также собирается pytest: при импорте ничего не выполняется.)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from nemus.gui.dialogs import SettingsDialog

    app = QApplication.instance() or QApplication([])  # noqa: F841
    dlg = SettingsDialog(None, core=None)
    dlg.show()
    app.processEvents()

    failures: list[str] = []

    def check(cond: bool, msg: str) -> None:
        print(("ok  " if cond else "FAIL") + " " + msg)
        if not cond:
            failures.append(msg)

    # страница вкладки и её размеры
    page = dlg._mcp_page
    check(page.width() > 500, f"ширина страницы {page.width()} > 500")
    check(page.height() > 300, f"высота страницы {page.height()} > 300")

    # верхняя строка: тумблер и ползунок влезают в ширину страницы
    check(
        dlg.chk_mcp_enabled.sizeHint().width() <= page.width(),
        f"тумблер {dlg.chk_mcp_enabled.sizeHint().width()}px <= {page.width()}px",
    )
    check(
        dlg.slider_mcp_refresh.width() <= page.width(),
        f"ползунок {dlg.slider_mcp_refresh.width()}px <= {page.width()}px",
    )
    check(bool(dlg.lbl_mcp_refresh_val.text()), "период подписан")

    # строка пресетов: комбо + 2 кнопки влезают в ширину страницы
    presets_w = (
        dlg.cb_mcp_presets.minimumSizeHint().width()
        + 200  # примерная ширина двух Ghost-кнопок
    )
    check(presets_w <= page.width(), f"строка пресетов ~{presets_w}px <= {page.width()}px")

    # добавление: карточка появляется и влезает по ширине в страницу
    check(dlg.lbl_mcp_empty.isHidden() is False, "пустой список показывает подсказку")
    dlg.cb_mcp_presets.setCurrentIndex(0)
    dlg._mcp_add_preset()
    check(len(dlg.mcp_cards) == 1, "карточка создана")
    check(dlg.lbl_mcp_empty.isHidden(), "подсказка пустоты спрятана")

    card = dlg.mcp_cards[0]
    app.processEvents()
    card_w, page_w = card.width(), page.width()
    check(0 < card_w <= page_w, f"карточка {card_w}px <= страница {page_w}px")

    # шапка карточки: кнопки влезают в её ширину
    for name in ("btn_probe", "btn_delete", "btn_expand"):
        btn = getattr(card, name)
        check(
            btn.minimumSizeHint().width() <= card_w + 40,
            f"{name}: нужно {btn.minimumSizeHint().width()}px, карточка {card_w}px",
        )

    # строка списка → шапка карточки: название и команда на месте
    text = f"{card.lbl_name.text()} {card.lbl_cmd.text()}"
    check("filesystem" in text.lower() or "npx" in text, f"шапка карточки: {text!r}")

    # форма свёрнута, раскрывается кнопкой «▾» и не вылезает за карточку
    check(card._form_wrap.isHidden(), "форма свёрнута по умолчанию")
    card.set_expanded(True)
    check(card._form_wrap.isHidden() is False, "форма раскрылась")
    app.processEvents()
    check(
        card.edit_name.width() <= card_w,
        f"поле названия {card.edit_name.width()}px <= карточка {card_w}px",
    )
    check(card.edit_cmd.text() == "npx", "команда пресета в форме")
    card.set_expanded(False)
    check(card._form_wrap.isHidden(), "форма свернулась обратно")

    dlg.close()
    print()
    if failures:
        print(f"ПРОБЛЕМЫ: {len(failures)}")
        for line in failures:
            print(" -", line)
        return 1
    print("ВСЁ ХОРОШО")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
