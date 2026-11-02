"""Тест retry-политики и fallback LLM, маскирования ключей в логах.

Регресс к багам:
- провайдер после двух неудачных попыток сразу уходил в fallback: секундный
  обрыв связи переключал модель, а это и лишние потраченные лимиты, и
  повторная отправка всей истории новому провайдеру;
- исчерпанные лимиты (429/квота) и неповторяемые ошибки (ключ/модель)
  ретраились так же, как временный сбой — 11 бессмысленных запросов;
- ошибка уже открытого стрима (например 503 от Gemini) не уходила в fallback
  и обрывала агента целиком («Сбой агента» вместо ответа резервной модели);
- трейсбеки с заголовками запросов писались в лог без маскирования ключей.

Запуск:  venv\\Scripts\\python.exe scripts\\test_llm_fallback.py
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import litellm  # noqa: E402

from nemus.config import get_config  # noqa: E402
from nemus.core.llm import (  # noqa: E402
    MAX_ATTEMPTS,
    MAX_BACKOFF,
    ChatTarget,
    LLMClient,
    _is_gemini_3_plus,
    error_kind,
    retry_delay,
)
from nemus.core.providers import Message, ProviderError  # noqa: E402
from nemus.utils.redact import redact_secrets  # noqa: E402


def _chunk(content: str | None = None, finish: str | None = None) -> SimpleNamespace:
    delta = SimpleNamespace(
        content=content, tool_calls=None, reasoning=None, reasoning_content=None
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)]
    )


def _client() -> LLMClient:
    cfg = get_config()
    cfg.llm.default_provider = "gemini"
    cfg.llm.default_model = "gemini/primary"
    cfg.llm.fallback = []
    client = LLMClient(cfg)
    client._fallbacks = [ChatTarget("openai", "openai/backup")]
    return client


def _messages() -> list[Message]:
    return [Message(role="system", content="s"), Message(role="user", content="привет")]


def test_midstream_error_falls_back() -> None:
    """Обрыв стрима до первой дельты → переходим на резервную модель."""
    calls: list[str] = []
    original = litellm.completion

    class _Broken:
        def __iter__(self):
            raise RuntimeError("503 from primary before any chunk")
            yield  # pragma: no cover

    def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "gemini/primary":
            return _Broken()
        return iter([_chunk("ответ "), _chunk("резервной модели", finish="stop")])

    litellm.completion = fake_completion
    try:
        # паузы между попытками отключаем — иначе тест ждал бы ~40 секунд
        with mock.patch("nemus.core.llm.time.sleep"):
            events = list(_client().stream(_messages()))
    finally:
        litellm.completion = original

    # основной провайдер ретраится MAX_ATTEMPTS раз, потом — резервный
    assert calls == ["gemini/primary"] * MAX_ATTEMPTS + ["openai/backup"], calls
    final = [e for e in events if e.kind == "final"]
    assert final and final[-1].response.content == "ответ резервной модели", events
    print(f"ok  обрыв стрима до дельт → {MAX_ATTEMPTS} ретраев, затем fallback")


def test_rate_limit_falls_back_immediately() -> None:
    """Исчерпанные лимиты (429) — фолбэк сразу, без единого повтора."""
    calls: list[str] = []
    original = litellm.completion

    class RateLimitError(Exception):  # имитация litellm.RateLimitError
        pass

    def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "gemini/primary":
            raise RateLimitError("429 Too Many Requests: rate limit exceeded")
        return iter([_chunk("ответ из резерва", finish="stop")])

    litellm.completion = fake_completion
    try:
        with mock.patch("nemus.core.llm.time.sleep"):
            events = list(_client().stream(_messages()))
    finally:
        litellm.completion = original

    assert calls == ["gemini/primary", "openai/backup"], calls
    final = [e for e in events if e.kind == "final"]
    assert final and final[-1].response.content == "ответ из резерва", events
    print("ok  лимиты исчерпаны → фолбэк сразу, без ретраев")


def test_fatal_error_falls_back_immediately() -> None:
    """Неверный ключ (401) — повторы бессмысленны, уходим сразу."""
    calls: list[str] = []
    original = litellm.completion

    def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "gemini/primary":
            raise RuntimeError("Incorrect API key provided: sk-xxx")
        return iter([_chunk("ответ из резерва", finish="stop")])

    litellm.completion = fake_completion
    try:
        with mock.patch("nemus.core.llm.time.sleep"):
            events = list(_client().stream(_messages()))
    finally:
        litellm.completion = original

    assert calls == ["gemini/primary", "openai/backup"], calls
    final = [e for e in events if e.kind == "final"]
    assert final and final[-1].response.content == "ответ из резерва", events
    print("ok  ошибка ключа → фолбэк без ретраев")


def test_time_budget_stops_hung_provider() -> None:
    """Провайдер, висящий по таймауту, уходит в фолбэк раньше 11-й попытки.

    Иначе 11 × request_timeout (120 с) = 22 минуты ожидания одного провайдера.
    """
    calls: list[str] = []
    original = litellm.completion

    def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "gemini/primary":
            raise RuntimeError("Read timed out")
        return iter([_chunk("ответ из резерва", finish="stop")])

    # время идёт по 120 с на попытку: первая съедает 120 с из бюджета 600 с,
    # вторая уходит за потолок — фолбэк раньше 11-й попытки
    ticks = iter([0.0, 120.0, 610.0])
    litellm.completion = fake_completion
    try:
        with mock.patch("nemus.core.llm.time.sleep"), mock.patch(
            "nemus.core.llm.time.monotonic", lambda: next(ticks, 9999.0)
        ):
            events = list(_client().stream(_messages()))
    finally:
        litellm.completion = original

    assert calls == ["gemini/primary", "gemini/primary", "openai/backup"], calls
    final = [e for e in events if e.kind == "final"]
    assert final and final[-1].response.content == "ответ из резерва", events
    print("ok  зависший по таймауту провайдер → фолбэк до 11-й попытки")


def test_slow_first_attempt_still_retries() -> None:
    """Долгая, но упавшая первая попытка не съедает все повторы (ретраи на 503).

    Регресс инцидента: бюджет 180 с целиком съедала одна медленная попытка,
    и 503 уходил в фолбэк с нулём повторов, хотя повтор был бы успешен.
    """
    calls: list[str] = []
    original = litellm.completion

    def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise RuntimeError("503 Service Unavailable: high demand")
        return iter([_chunk("успех после повтора", finish="stop")])

    # первая попытка заняла почти весь бюджет (500 с из 600) — при бюджете
    # в 180 с повторов не было бы вообще
    ticks = iter([0.0, 500.0])
    litellm.completion = fake_completion
    try:
        with mock.patch("nemus.core.llm.time.sleep"), mock.patch(
            "nemus.core.llm.time.monotonic", lambda: next(ticks, 9999.0)
        ):
            events = list(_client().stream(_messages()))
    finally:
        litellm.completion = original

    assert calls == ["gemini/primary", "gemini/primary"], calls
    final = [e for e in events if e.kind == "final"]
    assert final and final[-1].response.content == "успех после повтора", events
    print("ok  медленная первая попытка не съедает повторы: 503 вылечен ретраем")


def test_litellm_noise_suppressed() -> None:
    """litellm не мусорит в консоль: DeprecationWarning и Give Feedback глушатся."""
    from nemus.core.llm import _import_litellm, _LiteLLMNoiseFilter

    module = _import_litellm()
    assert module.suppress_debug_info is True, module.suppress_debug_info
    log = logging.getLogger("LiteLLM")
    assert any(isinstance(f, _LiteLLMNoiseFilter) for f in log.filters), log.filters

    probe = _LiteLLMNoiseFilter()

    def record(msg: str) -> logging.LogRecord:
        return logging.LogRecord("LiteLLM", logging.WARNING, __file__, 1, msg, (), None)

    noise = record(
        "DeprecationWarning: `temperature`, `top_p`, and `top_k` continue to "
        "function for Gemini 3+ (gemini-3.8-flash) but are planned for removal"
    )
    useful = record("полезное предупреждение litellm")
    assert not probe.filter(noise), "шум должен фильтроваться"
    assert probe.filter(useful), "полезные предупреждения не должны пропадать"
    print("ok  шум litellm подавлен, полезные предупреждения проходят")


def test_gemini_3_detection() -> None:
    """Сэмплинг снимается только с Gemini 3+: старые модели не задеты."""
    cases = [
        ("gemini/gemini-3.8-flash", True),
        ("gemini-3.0-pro-preview", True),
        ("gemini/gemini-2.0-flash", False),
        ("gemini/gemini-1.5-pro", False),
        ("gemini/gemini-pro", False),
        ("gemini/gemini-exp-1206", False),
        ("openai/gpt-4o-mini", False),
        ("anthropic/claude-3-5-sonnet", False),
        ("", False),
    ]
    for model, expected in cases:
        got = _is_gemini_3_plus(model)
        assert got is expected, f"{model}: ожидался {expected}, получен {got}"
    print("ok  определение Gemini 3+: старые модели остаются со своим сэмплингом")


def test_sampling_stripped_only_for_gemini_3() -> None:
    """temperature уходит только Gemini 3+; GPT/Claude/Gemini 1.5-2.0 — как были."""
    seen: list[dict] = []
    original = litellm.completion

    def fake_completion(**kwargs):
        seen.append(kwargs)
        return iter([_chunk("готово", finish="stop")])

    litellm.completion = fake_completion
    try:
        client = _client()
        client.set_model("gemini", "gemini/gemini-3.8-flash")
        list(client.stream(_messages()))
        client.set_model("openai", "openai/gpt-4o-mini")
        list(client.stream(_messages()))
    finally:
        litellm.completion = original

    assert len(seen) == 2, seen
    gemini3, legacy = seen
    assert "temperature" not in gemini3, gemini3
    assert "top_p" not in gemini3 and "top_k" not in gemini3, gemini3
    assert legacy["temperature"] == client.config.llm.temperature, legacy
    print(
        "ok  temperature: Gemini 3+ — без него, остальные — "
        f"{legacy['temperature']}"
    )


def test_error_classification() -> None:
    """429/квота → limit, ключ/модель → fatal, обрыв связи → transient."""
    cases = [
        (RuntimeError("Error code: 429"), "limit"),
        (RuntimeError("You exceeded your current quota"), "limit"),
        (RuntimeError("RESOURCE_EXHAUSTED: Quota exceeded"), "limit"),
        (RuntimeError("rate_limit_error"), "limit"),
        (RuntimeError("401 Unauthorized"), "fatal"),
        (RuntimeError("model not found: gpt-x"), "fatal"),
        (RuntimeError("Missing credentials. Please pass an `api_key`."), "fatal"),
        (RuntimeError("This model's maximum context length is 8192 tokens"), "fatal"),
        (RuntimeError("connection reset by peer"), "transient"),
        (RuntimeError("Read timed out"), "transient"),
        (RuntimeError("503 Service Unavailable"), "transient"),
        (RuntimeError("4030 токенов в истории"), "transient"),  # не код 403
    ]
    for exc, expected in cases:
        got = error_kind(exc)
        assert got == expected, f"{exc}: ожидался {expected}, получен {got}"
    print("ok  классификация ошибок: limit / fatal / transient")


def test_backoff_is_bounded() -> None:
    """Пауза растёт, но не превышает потолок — медленным не бывает."""
    delays = [retry_delay(n) for n in range(1, MAX_ATTEMPTS + 1)]
    assert delays == sorted(delays), delays  # растёт
    assert max(delays) <= MAX_BACKOFF, delays
    assert delays[0] <= 1.0, delays
    print(f"ok  бэкофф растёт и ограничен {MAX_BACKOFF} с (макс. {max(delays):.1f} с)")


def test_midstream_error_after_text_raises() -> None:
    """После отправленной части ответа повторный стрим её продублирует — ошибка."""
    original = litellm.completion

    class _BrokenAfterText:
        def __iter__(self):
            yield _chunk("частичный ")
            raise RuntimeError("connection reset mid-stream")

    litellm.completion = lambda **kwargs: _BrokenAfterText()
    try:
        list(_client().stream(_messages()))
        raise AssertionError("ожидался ProviderError")
    except ProviderError as exc:
        assert "стрим прерван" in str(exc), exc
    finally:
        litellm.completion = original
    print("ok  обрыв после дельты → понятная ProviderError, без тихого сбоя")


def test_all_targets_fail() -> None:
    """Когда упали и основной, и резервный стрим — общая ошибка провайдеров."""
    original = litellm.completion

    def fake_completion(**kwargs):
        raise RuntimeError(f"недоступен {kwargs['model']}")

    litellm.completion = fake_completion
    try:
        with mock.patch("nemus.core.llm.time.sleep"):
            list(_client().stream(_messages()))
        raise AssertionError("ожидался ProviderError")
    except ProviderError as exc:
        assert "Все провайдеры недоступны" in str(exc), exc
        assert "gemini/primary" in str(exc) and "openai/backup" in str(exc), exc
        # в сводке — число попыток, а не 11 копий одной ошибки
        assert str(exc).count("недоступен") == 2, exc
    finally:
        litellm.completion = original
    print("ok  все провайдеры недоступны → одна сводная ProviderError")


def test_complete_retries_then_falls_back() -> None:
    """Синхронный запрос: те же 11 попыток, затем резервная модель."""
    calls: list[str] = []
    original = litellm.completion

    def fake_completion(**kwargs):
        calls.append(kwargs["model"])
        if kwargs["model"] == "gemini/primary":
            raise RuntimeError("connection reset by peer")
        message = SimpleNamespace(content="синхронный ответ", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")],
            model=kwargs["model"],
            usage=None,
        )

    litellm.completion = fake_completion
    try:
        with mock.patch("nemus.core.llm.time.sleep"):
            resp = _client().complete(_messages())
    finally:
        litellm.completion = original

    assert calls == ["gemini/primary"] * MAX_ATTEMPTS + ["openai/backup"], calls
    assert resp.content == "синхронный ответ", resp.content
    print(f"ok  complete(): {MAX_ATTEMPTS} ретраев, затем fallback")


def test_redact_headers() -> None:
    """API-ключ в заголовке (и в записи словаря) маскируется."""
    plain = redact_secrets("x-goog-api-key: AIzaSECRETVALUE1234567890")
    assert "AIzaSECRETVALUE1234567890" not in plain and "***" in plain, plain

    in_dict = redact_secrets(
        "{'Content-Type': 'application/json', "
        "'x-goog-api-key': 'AQ.Ab8RN6JHR7longsecretvalue'}"
    )
    assert "AQ.Ab8RN6JHR7longsecretvalue" not in in_dict, in_dict
    assert "***" in in_dict, in_dict

    bearer = redact_secrets("Authorization: Bearer sk-VERYSECRETVALUE")
    assert "sk-VERYSECRETVALUE" not in bearer, bearer
    print("ok  ключи в заголовках маскируются")


def test_logger_redacts_traceback() -> None:
    """Трейсбек в файле лога печатается, но без ключа из текста исключения."""
    import tempfile

    from nemus.utils.logger import logger, setup_logging

    log_path = Path(tempfile.mkdtemp(prefix="nemus_log_")) / "probe.log"
    setup_logging("INFO", str(log_path))
    try:
        raise RuntimeError("boom: x-goog-api-key: AIzaTRACEBACKSECRET123456789")
    except RuntimeError:
        logger.exception("Сбой агента")
    logger.complete()

    text = log_path.read_text(encoding="utf-8")
    assert "Сбой агента" in text, text
    assert "Traceback" in text, text  # трейсбек сохранён
    assert "AIzaTRACEBACKSECRET123456789" not in text, "ключ утёк в лог"
    assert "***" in text, text
    print("ok  трейсбек пишется в лог, ключ замаскирован")


def main() -> int:
    test_error_classification()
    test_backoff_is_bounded()
    test_litellm_noise_suppressed()
    test_gemini_3_detection()
    test_sampling_stripped_only_for_gemini_3()
    test_time_budget_stops_hung_provider()
    test_slow_first_attempt_still_retries()
    test_midstream_error_falls_back()
    test_rate_limit_falls_back_immediately()
    test_fatal_error_falls_back_immediately()
    test_midstream_error_after_text_raises()
    test_all_targets_fail()
    test_complete_retries_then_falls_back()
    test_redact_headers()
    test_logger_redacts_traceback()
    print("\nВСЕ ТЕСТЫ ПРОШЛИ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
