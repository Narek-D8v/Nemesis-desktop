"""Тесты новых функций Telegram: документы, /pc, /doctor, фоновые команды.

Без сети и без Telegram. Запуск:
    venv\\Scripts\\python.exe scripts/test_telegram_extras.py
"""

from __future__ import annotations

import logging
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.getLogger("pypdf").setLevel(logging.ERROR)

from nemus.config import Config  # noqa: E402
from nemus.core.document_text import (  # noqa: E402
    DocumentError,
    document_context,
    extract_document,
)
from nemus.core.process_jobs import ProcessJobs  # noqa: E402
from nemus.core.telegram_health import doctor_report, pc_report  # noqa: E402
from nemus.core.tools.web import _is_blocked_ip  # noqa: E402


def _docx(path: Path, text: str) -> None:
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
        '2006/main"><w:body><w:p><w:r><w:t>'
        + text
        + "</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def test_documents() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        text = folder / "notes.txt"
        text.write_text("привет, документ", encoding="utf-8")
        assert "привет" in extract_document(text)

        table = folder / "table.csv"
        table.write_text("имя,возраст\nАня,30\nБоб,25", encoding="utf-8")
        csv_text = extract_document(table)
        assert "Аня" in csv_text and "|" in csv_text

        word = folder / "report.docx"
        _docx(word, "Первый абзац")
        assert "Первый абзац" in extract_document(word)

        context = document_context([text, word])
        assert "notes.txt" in context and "Первый абзац" in context

        broken = folder / "broken.pdf"
        broken.write_bytes(b"not a pdf")
        try:
            extract_document(broken)
        except DocumentError:
            pass
        else:
            raise AssertionError("битый PDF должен давать DocumentError")
    print("ok  документы: TXT, CSV, DOCX и общий контекст")


def test_pc_and_doctor() -> None:
    report = pc_report(Path.cwd())
    assert "CPU" in report and "RAM" in report and "диск" in report
    cfg = Config()
    cfg.telegram.token = "secret-token"
    cfg.telegram.allowed_users = [1]
    bot = SimpleNamespace(
        config=cfg,
        cfg=cfg.telegram,
        core=SimpleNamespace(
            llm=SimpleNamespace(provider="gemini", model="gemini/test")
        ),
        username="test_bot",
        started_at=time.monotonic(),
        _webapp=SimpleNamespace(running=True, bound_port=8765),
        _tunnel=None,
        _jobs=None,
    )
    doctor = doctor_report(bot)
    assert "Самодиагностика" in doctor
    assert "Web App" in doctor and "LLM" in doctor and "MCP" in doctor
    assert "secret-token" not in doctor, "токен не должен попадать в отчёт"
    print("ok  /pc и /doctor собирают отчёт без падения")


def test_process_jobs() -> None:
    done: list[int] = []
    jobs = ProcessJobs(".", on_done=lambda job: done.append(job.exit_code or 0))
    job = jobs.start(
        f'"{sys.executable}" -c "print(\'job-ok\')"',
        chat_id=1,
    )
    deadline = time.time() + 10
    while job.running and time.time() < deadline:
        time.sleep(0.05)
    assert not job.running, "короткая команда должна завершиться"
    assert job.exit_code == 0, job.exit_code
    assert "job-ok" in job.tail(), job.tail()
    assert done == [0], done

    sleeper = jobs.start(f'"{sys.executable}" -c "import time; time.sleep(30)"', 2)
    time.sleep(0.3)
    assert sleeper.running
    assert jobs.stop(sleeper.job_id), "stop должен вернуть True для живой задачи"
    deadline = time.time() + 10
    while sleeper.running and time.time() < deadline:
        time.sleep(0.05)
    assert not sleeper.running, "задача должна остановиться"
    jobs.stop_all()
    print("ok  фоновые команды: вывод, код возврата, уведомление и остановка")


def test_ssrf_guard() -> None:
    assert _is_blocked_ip("127.0.0.1")
    assert _is_blocked_ip("192.168.1.1")
    assert _is_blocked_ip("::1")
    assert not _is_blocked_ip("8.8.8.8")
    print("ok  SSRF-защита не пускает локальные адреса")


if __name__ == "__main__":
    test_documents()
    test_pc_and_doctor()
    test_process_jobs()
    test_ssrf_guard()
    print("\nTELEGRAM EXTRAS TESTS PASSED")
