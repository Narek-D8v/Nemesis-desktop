"""Инструменты скиллов: чтение чужих инструкций и создание своих."""

from __future__ import annotations

from nemus.core.skills import (
    DEFAULT_SKILLS_DIR,
    get_skill,
    load_skills,
    save_skill,
    search_skills,
    skill_highlights,
    skill_plugin,
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


class FindSkillsTool(Tool):
    """Ищет установленные скиллы и плагины по теме задачи."""

    name = "find_skills"
    description = (
        "Ищет по УСТАНОВЛЕННЫМ скиллам и плагинам: по названию, описанию, "
        "заголовкам и тексту инструкций. ОБЯЗАТЕЛЬНО вызывай ПЕРВЫМ делом для "
        "любой задачи на код, видео, сайт, дизайн, презентацию, документ или "
        "другой создаваемый артефакт — до load_skill и до первого "
        "производственного действия. Возвращает кандидатов с описаниями. Если "
        "под задачу подходят несколько скиллов — загрузи ВСЕ подходящие через "
        "load_skill. Если это разные подходы к одному результату (например, "
        "HyperFrames и Remotion для видео) — НЕ выбирай первый молча: покажи "
        "пользователю 2–3 варианта с плюсами/минусами и спроси, какой делать "
        "(или оба)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Тема задачи простыми словами: «видео», «сайт», "
                    "«рефакторинг python», «презентация»."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Сколько кандидатов вернуть (по умолчанию 10).",
            },
        },
        "required": ["query"],
    }
    risk = RiskLevel.SAFE

    #: папка со скиллами (инъекция из конфига в factory._inject_settings)
    skills_dir: str = DEFAULT_SKILLS_DIR
    #: отключённые в настройках скиллы — их модель не должна видеть
    disabled: list[str] | None = None

    def execute(self, query: str = "", limit: int | str = 10, **_: object) -> ToolResult:
        query = str(query or "").strip()
        if not query:
            return ToolResult.failure(
                "Укажи тему задачи в query: «видео», «сайт», «код», «презентация»."
            )
        try:
            count = int(limit)
        except (TypeError, ValueError):
            count = 10
        count = max(1, min(20, count))
        try:
            skills = load_skills(self.skills_dir, disabled=self.disabled or ())
        except OSError as exc:
            return ToolResult.failure(f"Не удалось прочитать скиллы: {exc}")
        matches = search_skills(query, skills, limit=count)
        if not matches:
            return ToolResult.success(
                f"По запросу «{query}» установленных скиллов и плагинов не "
                "нашлось. Не выдумывай скилл. Проверь список инструментов, "
                "поищи подход через web_search или сделай своим ходом; если "
                "задача будет повторяться — сохрани навык через create_skill."
            )

        lines = [f"Кандидаты по запросу «{query}» ({len(matches)}):"]
        for index, skill in enumerate(matches, start=1):
            plugin = skill_plugin(skill.path, self.skills_dir)
            kind = "команда плагина" if skill.kind == "command" else "скилл"
            if plugin:
                kind += f", плагин «{plugin}»"
            description = (skill.description or "без описания").strip()
            if len(description) > 240:
                description = description[:237].rstrip() + "…"
            lines.append(f"{index}. {skill.name} ({kind}) — {description}")
            highlights = skill_highlights(skill.body, 5)
            if highlights:
                lines.append("   Состав: " + "; ".join(highlights))
        lines.extend(
            [
                "",
                "Дальше: загрузи load_skill(имя) ВСЕ подходящие скиллы, а не "
                "только первый. Если найденные скиллы — альтернативные способы "
                "сделать одно и то же (HyperFrames/Remotion, разные движки, "
                "разные стили), сначала покажи пользователю варианты A/B/C с "
                "плюсами и минусами и спроси, какой делать или нужны ли оба.",
            ]
        )
        return ToolResult.success("\n".join(lines), data={
            "query": query,
            "names": [skill.name for skill in matches],
        })


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
    """Инструменты поиска, чтения и создания скиллов."""
    return [FindSkillsTool(), LoadSkillTool(), CreateSkillTool()]


__all__ = ["CreateSkillTool", "FindSkillsTool", "LoadSkillTool", "skill_tools"]
