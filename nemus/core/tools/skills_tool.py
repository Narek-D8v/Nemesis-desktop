"""Инструменты скиллов: чтение чужих инструкций и создание своих."""

from __future__ import annotations

from nemus.core.skills import (
    DEFAULT_SKILLS_DIR,
    get_skill,
    load_skills,
    save_skill,
    skills_root,
)
from nemus.core.tools.base import RiskLevel, Tool, ToolResult


class LoadSkillTool(Tool):
    """Отдаёт полный текст скилла (SKILL.md) и список его файлов."""

    name = "load_skill"
    description = (
        "Читает скилл (навык в формате Claude Agent Skills) целиком: инструкцию "
        "из SKILL.md, её описание и файлы, лежащие рядом (скрипты, шаблоны). "
        "Скиллы перечислены в системном промпте — там только названия и описания, "
        "полный текст подгружается этим инструментом, когда задача попадает под "
        "описание скилла. Вызывай до начала работы, а не после."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Имя скилла — как в списке из системного промпта.",
            }
        },
        "required": ["name"],
    }
    risk = RiskLevel.SAFE

    #: папка со скиллами (инъекция из конфига в factory._inject_settings)
    skills_dir: str = DEFAULT_SKILLS_DIR
    #: отключённые в настройках скиллы — их модель не должна видеть
    disabled: list[str] | None = None

    def execute(self, name: str = "", **_: object) -> ToolResult:
        skill = get_skill(name, self.skills_dir, self.disabled or ())
        if skill is None:
            available = ", ".join(
                s.name for s in load_skills(self.skills_dir, self.disabled or ())
            )
            found = f"Доступные скиллы: {available}." if available else (
                "Скиллов пока нет."
            )
            return ToolResult.failure(
                f"Скилл «{name}» не найден. {found} Не выдумывай содержимое — "
                "сделай работу своим ходом или создай скилл через create_skill."
            )

        files = (
            "\nФайлы скилла (пути относительно "
            f"{skill.path.as_posix()}):\n"
            + "\n".join(f"- {f}" for f in skill.files)
            if skill.files
            else ""
        )
        allowed = (
            f"\nРекомендуемые инструменты: {', '.join(skill.allowed_tools)}."
            if skill.allowed_tools
            else ""
        )
        return ToolResult.success(
            f"Скилл «{skill.name}»: {skill.description}\n\n"
            f"Инструкция (SKILL.md):\n{skill.body}{allowed}{files}",
            data={"name": skill.name, "path": skill.path.as_posix()},
        )


class CreateSkillTool(Tool):
    """Сохраняет выученный навык в .agents/skills/<имя>/SKILL.md."""

    name = "create_skill"
    description = (
        "Сохраняет навык (скилл формата Claude Agent Skills) на диск, чтобы в "
        "следующий раз не объяснять задачу заново: создаётся папка с файлом "
        "SKILL.md (frontmatter name/description + тело инструкции). Используй, "
        "когда пользователь научил тебя делать что-то по определённой "
        "процедуре, или когда повторяющуюся задачу удобнее описать один раз. "
        "Тело пиши как чёткую пошаговую инструкцию (можно в markdown), "
        "описание — одним-двумя предложениями: по нему модель поймёт, когда "
        "скилл применять."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Имя скилла латиницей, напр. pdf-tools "
                    "(из него строится имя папки)."
                ),
            },
            "description": {
                "type": "string",
                "description": "Краткое описание: когда применять этот скилл.",
            },
            "content": {
                "type": "string",
                "description": "Полный текст инструкции (тело SKILL.md).",
            },
        },
        "required": ["name", "description", "content"],
    }
    risk = RiskLevel.MEDIUM

    skills_dir: str = DEFAULT_SKILLS_DIR

    def execute(
        self,
        name: str = "",
        description: str = "",
        content: str = "",
        **_: object,
    ) -> ToolResult:
        try:
            skill = save_skill(name, description, content, self.skills_dir)
        except ValueError as exc:
            return ToolResult.failure(str(exc))
        except OSError as exc:
            return ToolResult.failure(
                f"Не удалось записать скилл в {skills_root(self.skills_dir)}: {exc}"
            )
        return ToolResult.success(
            f"Скилл «{skill.name}» сохранён: {skill.skill_file.as_posix()}. "
            "Он сразу попадёт в список скиллов в системном промпте."
        )


def skill_tools() -> list[Tool]:
    """Инструменты чтения и создания скиллов."""
    return [LoadSkillTool(), CreateSkillTool()]


__all__ = ["CreateSkillTool", "LoadSkillTool", "skill_tools"]
