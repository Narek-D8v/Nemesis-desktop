"""Извлечение текста из присланных в Telegram документов.

Модуль не зависит от Telegram: принимает путь и возвращает текст. Для PDF
нужен ``pypdf``; DOCX/PPTX/XLSX разбираются штатным ``zipfile``, поэтому
офисные форматы работают без тяжёлых зависимостей.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

#: сколько символов отдаём с одного файла по умолчанию
DEFAULT_MAX_CHARS = 12_000
#: текстовые форматы, которые можно прочитать как есть
TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".rst",
    ".log",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".csv",
    ".tsv",
    ".xml",
    ".html",
    ".htm",
    ".py",
    ".js",
    ".ts",
    ".jsx",
    ".tsx",
    ".css",
    ".sql",
    ".sh",
    ".bat",
    ".ps1",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".go",
    ".rs",
}
#: форматы, которые умеем извлекать
SUPPORTED_SUFFIXES = TEXT_SUFFIXES | {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
}

_NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_NS_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_NS_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"


class DocumentError(RuntimeError):
    """Документ не удалось прочитать."""


def _decode(raw: bytes) -> str:
    """Декодирует текст, перебирая типичные кодировки русских файлов."""
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "koi8-r"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "\n\n…[текст обрезан]"
    return text


def _read_text_file(path: Path, limit: int) -> str:
    text = _decode(path.read_bytes())
    if path.suffix.lower() in {".csv", ".tsv"}:
        return _read_csv(text, path.suffix.lower(), limit)
    if path.suffix.lower() in {".html", ".htm"}:
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(text, "html.parser")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            return _clip(soup.get_text("\n", strip=True), limit)
        except Exception:  # noqa: BLE001
            pass
    return _clip(text, limit)


def _read_csv(text: str, suffix: str, limit: int) -> str:
    """Превращает CSV/TSV в компактную текстовую таблицу."""
    delimiter = "\t" if suffix == ".tsv" else ","
    try:
        sample = text[:4096]
        delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        pass
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[str] = []
    for index, row in enumerate(reader):
        if index >= 200:
            rows.append("…[строки обрезаны]")
            break
        cells = [cell.strip().replace("\n", " ") for cell in row[:20]]
        rows.append(" | ".join(cells))
    return _clip("\n".join(rows), limit)


def _read_pdf(path: Path, limit: int) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - зависит от установки
        raise DocumentError(
            "для PDF нужен пакет pypdf: pip install pypdf"
        ) from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as exc:  # noqa: BLE001
                raise DocumentError("PDF защищён паролем") from exc
        pages: list[str] = []
        for page_number, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001
                text = f"[страница {page_number}: не прочитана: {exc}]"
            if text.strip():
                pages.append(f"--- стр. {page_number} ---\n{text.strip()}")
            if sum(len(item) for item in pages) >= limit * 2:
                break
    except DocumentError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentError(f"не удалось открыть PDF: {exc}") from exc
    if not pages:
        raise DocumentError("в PDF нет текстового слоя (возможно, это сканы)")
    return _clip("\n\n".join(pages), limit)


def _read_docx(path: Path, limit: int) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            if "word/document.xml" not in archive.namelist():
                raise DocumentError("в DOCX нет word/document.xml")
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except DocumentError:
        raise
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise DocumentError(f"DOCX повреждён: {exc}") from exc
    lines: list[str] = []
    for paragraph in root.iter(f"{_NS_W}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{_NS_W}t"))
        if text.strip():
            lines.append(text.strip())
    if not lines:
        raise DocumentError("в DOCX не найден текст")
    return _clip("\n".join(lines), limit)


def _read_pptx(path: Path, limit: int) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            slides = sorted(
                (
                    name
                    for name in archive.namelist()
                    if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)
                ),
                key=lambda name: int(re.search(r"(\d+)", name).group(1)),  # type: ignore[union-attr]
            )
            if not slides:
                raise DocumentError("в PPTX не найдены слайды")
            chunks: list[str] = []
            for slide_number, name in enumerate(slides, 1):
                root = ElementTree.fromstring(archive.read(name))
                texts = [
                    node.text.strip()
                    for node in root.iter(f"{_NS_A}t")
                    if node.text and node.text.strip()
                ]
                if texts:
                    chunks.append(f"--- слайд {slide_number} ---\n" + "\n".join(texts))
                if sum(len(item) for item in chunks) >= limit * 2:
                    break
    except DocumentError:
        raise
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise DocumentError(f"PPTX повреждён: {exc}") from exc
    if not chunks:
        raise DocumentError("в PPTX не найден текст")
    return _clip("\n\n".join(chunks), limit)


def _read_xlsx(path: Path, limit: int) -> str:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                for item in root.iter(f"{_NS_S}si"):
                    shared.append(
                        "".join(node.text or "" for node in item.iter(f"{_NS_S}t"))
                    )
            sheets = sorted(
                (
                    name
                    for name in names
                    if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", name)
                ),
                key=lambda name: int(re.search(r"(\d+)", name).group(1)),  # type: ignore[union-attr]
            )
            if not sheets:
                raise DocumentError("в XLSX не найдены листы")
            chunks: list[str] = []
            for sheet_number, name in enumerate(sheets, 1):
                root = ElementTree.fromstring(archive.read(name))
                rows: list[str] = []
                for row_index, row in enumerate(root.iter(f"{_NS_S}row")):
                    if row_index >= 200:
                        rows.append("…[строки обрезаны]")
                        break
                    values: list[str] = []
                    for cell in row.iter(f"{_NS_S}c"):
                        value_node = cell.find(f"{_NS_S}v")
                        value = value_node.text if value_node is not None else ""
                        if cell.get("t") == "s" and value.isdigit():
                            index = int(value)
                            value = shared[index] if index < len(shared) else ""
                        elif cell.get("t") == "inlineStr":
                            value = "".join(
                                node.text or ""
                                for node in cell.iter(f"{_NS_S}t")
                            )
                        values.append((value or "").strip())
                    if any(values):
                        rows.append(" | ".join(values))
                if rows:
                    chunks.append(f"--- лист {sheet_number} ---\n" + "\n".join(rows))
                if sum(len(item) for item in chunks) >= limit * 2:
                    break
    except DocumentError:
        raise
    except (OSError, zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise DocumentError(f"XLSX повреждён: {exc}") from exc
    if not chunks:
        raise DocumentError("в XLSX не найден текст")
    return _clip("\n\n".join(chunks), limit)


def extract_document(path: Path | str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Возвращает текст документа или бросает :class:`DocumentError`."""
    file = Path(path)
    if not file.is_file():
        raise DocumentError("файл не найден")
    suffix = file.suffix.lower()
    limit = max(500, int(max_chars))
    if suffix in TEXT_SUFFIXES:
        return _read_text_file(file, limit)
    if suffix == ".pdf":
        return _read_pdf(file, limit)
    if suffix == ".docx":
        return _read_docx(file, limit)
    if suffix == ".pptx":
        return _read_pptx(file, limit)
    if suffix == ".xlsx":
        return _read_xlsx(file, limit)
    raise DocumentError(f"формат {suffix or 'без расширения'} не поддерживается")


def document_context(paths: list[Path], max_chars: int = 8_000) -> str:
    """Текст нескольких документов для промпта агента, с заголовками файлов."""
    blocks: list[str] = []
    budget = max_chars * 2
    for path in paths:
        try:
            text = extract_document(path, max_chars=max_chars)
        except DocumentError as exc:
            blocks.append(f"[{path.name}: {exc}]")
            continue
        block = f"Содержимое файла «{path.name}»:\n{text}"
        if sum(len(item) for item in blocks) + len(block) > budget:
            blocks.append(f"[{path.name}: пропущен, общий лимит текста]")
            break
        blocks.append(block)
    return "\n\n".join(blocks)
