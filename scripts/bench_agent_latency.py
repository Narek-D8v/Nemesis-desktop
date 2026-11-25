"""Замер задержки простой реплики и проверка быстрого пути агента.

Только заглушки: никаких реальных LLM, сети, загрузок и инструментов.
Быстрый путь измеряется на «Немуся?» с включённым характером. Для честного
сравнения полный цикл прогоняется тем же текстом с фиктивным image_url:
это реальная ветка ``Agent.run``, в которой быстрый путь исключён правилом
``images``. Время LLM и задержки повторов провайдера заданы заглушками.

Запуск:  venv\\Scripts\\python.exe scripts\\bench_agent_latency.py
"""

from __future__ import annotations

import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import nemus.core.agent as agent_module  # noqa: E402
from nemus.config import Config  # noqa: E402
from nemus.core.agent import Agent, _character_prompt_block  # noqa: E402
from nemus.core.llm import StreamEvent  # noqa: E402
from nemus.core.permissions import PermissionManager  # noqa: E402
from nemus.core.providers import LLMResponse, ProviderError  # noqa: E402
from nemus.core.tools.registry import ToolRegistry  # noqa: E402

FAST_REPLY = "Привет! Я тут, рада тебя слышать."
FULL_REPLY = "Ответ полного цикла (заглушка)."
_PHASES = ("_refresh_system_prompt", "_apply_language_hint", "_apply_skill_context")


class StubLLM:
    """LLM без сети: быстрый ответ и полный цикл с настраиваемыми задержками."""

    def __init__(
        self,
        *,
        config: Config | None = None,
        complete_delay: float = 0.010,
        stream_delay: float = 0.020,
        fail_stream_calls: int = 0,
    ) -> None:
        self.config = config
        self.complete_delay = complete_delay
        self.stream_delay = stream_delay
        self.fail_stream_calls = fail_stream_calls
        self.complete_calls = 0
        self.stream_calls = 0
        self.complete_seconds = 0.0
        self.stream_seconds = 0.0
        self.last_messages: list[Any] | None = None
        self.character_block = ""

    def complete(self, messages: list[Any], **overrides: Any) -> LLMResponse:
        self.complete_calls += 1
        self.last_messages = messages
        # Считаем блок в момент вызова: состояние эмоций уже обновлено
        # _observe_emotions(), как и перед настоящим запросом к модели.
        if self.config is not None:
            self.character_block = _character_prompt_block(self.config)
        started = time.perf_counter()
        time.sleep(self.complete_delay)
        self.complete_seconds += time.perf_counter() - started
        return LLMResponse(
            content=FAST_REPLY,
            model=str(overrides.get("model") or "stub-fast"),
        )

    def stream(self, messages: list[Any], tools: Any = None, **overrides: Any):
        self.stream_calls += 1
        started = time.perf_counter()
        time.sleep(self.stream_delay)
        self.stream_seconds += time.perf_counter() - started
        if self.stream_calls <= self.fail_stream_calls:
            raise ProviderError("stubbed transient provider failure", retryable=True)
        yield StreamEvent(kind="content", text=FULL_REPLY)
        yield StreamEvent(kind="final", response=LLMResponse(content=FULL_REPLY))


def _isolated_config() -> Config:
    """Config без записи в рабочие файлы репозитория."""
    cfg = Config()
    state = Path(tempfile.gettempdir()) / f"nemus_task9_{uuid.uuid4().hex}"
    cfg.character.state_file = str(state / "emotions.json")
    cfg.character.diary_file = str(state / "diary.md")
    return cfg


def build_agent(cfg: Config, llm: StubLLM | None = None) -> Agent:
    """Агент с пустым реестром инструментов и заглушкой LLM."""
    llm = llm or StubLLM()
    if llm.config is None:
        llm.config = cfg
    return Agent(
        cfg,
        llm=llm,  # type: ignore[arg-type]
        registry=ToolRegistry(),
        permissions=PermissionManager(cfg),
        memory=None,
    )


def test_simple_request_uses_fast_path_with_character_enabled() -> None:
    """Простая реплика закрывается быстрым путём даже при включённом характере."""
    cfg = _isolated_config()
    cfg.character.enabled = True
    llm = StubLLM()
    agent = build_agent(cfg, llm)
    events = list(agent.run("Немуся?", stop=None))
    assert any(e.kind == "final" for e in events)
    assert not any(e.kind == "tool_call" for e in events)
    # Дополнительно фиксируем, что это именно быстрый путь, а не везение:
    # полный цикл даже не начинался (prompt refresh и tool-схемы не трогались).
    assert llm.complete_calls == 1, llm.complete_calls
    assert llm.stream_calls == 0, llm.stream_calls
    # Проверяем и содержимое быстрого промпта: характер не должен тихо
    # исчезнуть из системного сообщения при включённом character.enabled.
    assert llm.last_messages is not None, "быстрый путь не передал сообщения в LLM"
    assert llm.last_messages[0].role == "system"
    assert llm.character_block, "_character_prompt_block вернул пусто при включённом характере"
    assert llm.character_block in str(llm.last_messages[0].content), (
        "в быстром системном промпте нет блока характера"
    )


def _instrument(agent: Agent) -> dict[str, float]:
    """Подсчитать время фаз полного цикла, не меняя код агента."""
    timings = dict.fromkeys(_PHASES, 0.0)
    for name in _PHASES:
        original = getattr(agent, name)

        def timed(*args: Any, _name: str = name, _original: Any = original, **kwargs: Any):
            started = time.perf_counter()
            try:
                return _original(*args, **kwargs)
            finally:
                timings[_name] += time.perf_counter() - started

        setattr(agent, name, timed)
    return timings


def _measure(
    text: str,
    *,
    images: list[str] | None,
    fail_stream_calls: int,
) -> dict[str, Any]:
    cfg = _isolated_config()
    llm = StubLLM(fail_stream_calls=fail_stream_calls)
    agent = build_agent(cfg, llm)
    phases = _instrument(agent)
    started = time.perf_counter()
    events = list(agent.run(text, images=images, stop=None))
    total = time.perf_counter() - started
    return {
        "total": total,
        "phases": phases,
        "llm_seconds": llm.complete_seconds + llm.stream_seconds,
        "complete_calls": llm.complete_calls,
        "stream_calls": llm.stream_calls,
        "events": [event.kind for event in events],
        "complete_delay": llm.complete_delay,
        "stream_delay": llm.stream_delay,
        "forced_failures": llm.fail_stream_calls,
    }


def _print(label: str, result: dict[str, Any]) -> None:
    phases = result["phases"]
    overhead = result["total"] - result["llm_seconds"]
    print(f"{label:<8} total={result['total']:.4f}s  llm={result['llm_seconds']:.4f}s"
          f"  agent_overhead={overhead:.4f}s")
    print(
        f"         phases: refresh={phases['_refresh_system_prompt']:.4f}s"
        f"  language={phases['_apply_language_hint']:.4f}s"
        f"  skills={phases['_apply_skill_context']:.4f}s"
    )
    print(
        f"         events={result['events']}"
        f"  complete={result['complete_calls']}"
        f"  stream={result['stream_calls']}"
    )


def main() -> int:
    test_simple_request_uses_fast_path_with_character_enabled()
    print("ok  regression: final without tool_call, fast path only (character enabled)")

    # Реальные ветки кода, но LLM и задержки повторов — заглушки.
    fast = _measure("Немуся?", images=None, fail_stream_calls=0)
    with patch.object(agent_module, "PROVIDER_RETRY_DELAYS", (0.002, 0.002)):
        full = _measure(
            "Немуся?",
            images=["data:image/png;base64,AAAA"],
            fail_stream_calls=1,
        )

    print()
    print(
        f"stub timings: fast complete={fast['complete_delay'] * 1000:.0f}ms; "
        f"full stream={full['stream_delay'] * 1000:.0f}ms x "
        f"{1 + full['forced_failures']} (one stubbed transient retry, "
        "retry delay patched to 2ms)"
    )
    print("full loop forced with a stub image attachment (images excludes fast path)")
    print()
    _print("fast", fast)
    _print("full", full)
    if fast["total"] and full["total"]:
        print(f"ratio    full/fast={full['total'] / fast['total']:.1f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
