"""Разбитые GGUF: разбор имён частей и склейка в один файл.

Hugging Face хранит большие модели как набор самостоятельных GGUF-шардов
``model-00001-of-00002.gguf``. Это не куски одного файла: каждый шард имеет
свой заголовок и метаданные. Поэтому ``cat`` здесь не подходит — модуль
пересобирает общий заголовок и переносит тензоры в правильном порядке.
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

GGUF_MAGIC = b"GGUF"
#: тип значения метаданных GGUF
_GGUF_UINT8 = 0
_GGUF_INT8 = 1
_GGUF_UINT16 = 2
_GGUF_INT16 = 3
_GGUF_UINT32 = 4
_GGUF_INT32 = 5
_GGUF_FLOAT32 = 6
_GGUF_BOOL = 7
_GGUF_STRING = 8
_GGUF_ARRAY = 9
_GGUF_UINT64 = 10
_GGUF_INT64 = 11
_GGUF_FLOAT64 = 12

_FIXED_SIZES = {
    _GGUF_UINT8: 1,
    _GGUF_INT8: 1,
    _GGUF_UINT16: 2,
    _GGUF_INT16: 2,
    _GGUF_UINT32: 4,
    _GGUF_INT32: 4,
    _GGUF_FLOAT32: 4,
    _GGUF_BOOL: 1,
    _GGUF_UINT64: 8,
    _GGUF_INT64: 8,
    _GGUF_FLOAT64: 8,
}

_SPLIT_RE = re.compile(
    r"^(?P<prefix>.*)-(?P<index>\d{5})-of-(?P<count>\d{5})\.gguf$",
    re.IGNORECASE,
)
COPY_CHUNK = 4 * 1024 * 1024
DEFAULT_ALIGNMENT = 32


class GGUFMergeError(RuntimeError):
    """Не удалось разобрать или склеить GGUF-шарды."""


class GGUFMergeCancelled(GGUFMergeError):
    """Склейка отменена; временный файл удалён."""


@dataclass(frozen=True)
class SplitPart:
    prefix: str
    index: int
    count: int


@dataclass(frozen=True)
class _Tensor:
    name: str
    dims: tuple[int, ...]
    ggml_type: int
    offset: int
    new_offset: int = 0


@dataclass
class _Shard:
    path: Path
    version: int
    kvs: list[tuple[str, int, bytes]]
    tensors: list[_Tensor]
    data_start: int
    file_size: int


def split_part(filename: str) -> SplitPart | None:
    """Разбирает имя шарда; ``None`` для обычного GGUF."""
    match = _SPLIT_RE.match(Path(filename or "").name)
    if match is None:
        return None
    index = int(match.group("index"))
    count = int(match.group("count"))
    if count < 2 or index < 1 or index > count:
        return None
    return SplitPart(prefix=match.group("prefix"), index=index, count=count)


def is_split_gguf(filename: str) -> bool:
    """Часть разбитого на несколько файлов GGUF."""
    return split_part(filename) is not None


def shard_filenames(filename: str) -> list[str]:
    """Все имена шардов одной группы, включая переданное имя."""
    part = split_part(filename)
    if part is None:
        return [filename]
    return [
        f"{part.prefix}-{index:05d}-of-{part.count:05d}.gguf"
        for index in range(1, part.count + 1)
    ]


def merged_filename(filename: str) -> str:
    """Имя итогового файла без суффикса ``-00001-of-00002``."""
    part = split_part(filename)
    if part is None:
        return filename
    return f"{part.prefix}.gguf"


def _read_exact(handle, size: int) -> bytes:
    data = handle.read(size)
    if len(data) != size:
        raise GGUFMergeError("GGUF-файл оборван или имеет неверный заголовок.")
    return data


def _read_u32(handle) -> int:
    return struct.unpack("<I", _read_exact(handle, 4))[0]


def _read_u64(handle) -> int:
    return struct.unpack("<Q", _read_exact(handle, 8))[0]


def _write_u32(handle, value: int) -> None:
    handle.write(struct.pack("<I", int(value)))


def _write_u64(handle, value: int) -> None:
    handle.write(struct.pack("<Q", int(value)))


def _write_string(handle, text: str) -> None:
    encoded = text.encode("utf-8")
    _write_u64(handle, len(encoded))
    handle.write(encoded)


def _read_string(handle) -> str:
    size = _read_u64(handle)
    if size > 64 * 1024 * 1024:
        raise GGUFMergeError("Слишком большая строка в метаданных GGUF.")
    try:
        return _read_exact(handle, size).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GGUFMergeError("В метаданных GGUF неверная UTF-8 строка.") from exc


def _read_value_raw(handle, value_type: int) -> bytes:
    fixed = _FIXED_SIZES.get(value_type)
    if fixed is not None:
        return _read_exact(handle, fixed)
    if value_type == _GGUF_STRING:
        size = _read_u64(handle)
        if size > 1024 * 1024 * 1024:
            raise GGUFMergeError("Слишком большая строка в метаданных GGUF.")
        return struct.pack("<Q", size) + _read_exact(handle, size)
    if value_type == _GGUF_ARRAY:
        item_type = _read_u32(handle)
        count = _read_u64(handle)
        if count > 100_000_000:
            raise GGUFMergeError("Слишком большой массив в метаданных GGUF.")
        chunks = [struct.pack("<I", item_type), struct.pack("<Q", count)]
        for _ in range(count):
            chunks.append(_read_value_raw(handle, item_type))
        return b"".join(chunks)
    raise GGUFMergeError(f"Неизвестный тип метаданных GGUF: {value_type}.")


def _meta_int(kvs: Iterable[tuple[str, int, bytes]], key: str) -> int | None:
    for name, value_type, raw in kvs:
        if name != key:
            continue
        if value_type == _GGUF_UINT8 and len(raw) == 1:
            return raw[0]
        if value_type == _GGUF_INT8 and len(raw) == 1:
            return struct.unpack("<b", raw)[0]
        if value_type == _GGUF_UINT16 and len(raw) == 2:
            return struct.unpack("<H", raw)[0]
        if value_type == _GGUF_INT16 and len(raw) == 2:
            return struct.unpack("<h", raw)[0]
        if value_type == _GGUF_UINT32 and len(raw) == 4:
            return struct.unpack("<I", raw)[0]
        if value_type == _GGUF_UINT64 and len(raw) == 8:
            return struct.unpack("<Q", raw)[0]
        if value_type == _GGUF_INT32 and len(raw) == 4:
            return struct.unpack("<i", raw)[0]
        if value_type == _GGUF_INT64 and len(raw) == 8:
            return struct.unpack("<q", raw)[0]
    return None


def _alignment(kvs: Iterable[tuple[str, int, bytes]]) -> int:
    value = _meta_int(kvs, "general.alignment")
    if not value or value < 1 or value > 1024 * 1024:
        return DEFAULT_ALIGNMENT
    return value


def _align_up(value: int, alignment: int) -> int:
    return (value + alignment - 1) // alignment * alignment


def _parse_shard(path: Path) -> _Shard:
    try:
        file_size = path.stat().st_size
        with path.open("rb") as handle:
            if _read_exact(handle, 4) != GGUF_MAGIC:
                raise GGUFMergeError(f"{path.name}: это не GGUF-файл.")
            version = _read_u32(handle)
            if version not in (2, 3):
                raise GGUFMergeError(
                    f"{path.name}: поддерживаются GGUF версии 2 и 3, "
                    f"а файл версии {version}."
                )
            tensor_count = _read_u64(handle)
            kv_count = _read_u64(handle)
            if tensor_count > 10_000_000 or kv_count > 1_000_000:
                raise GGUFMergeError(f"{path.name}: подозрительно большой заголовок.")
            kvs: list[tuple[str, int, bytes]] = []
            for _ in range(kv_count):
                key = _read_string(handle)
                value_type = _read_u32(handle)
                kvs.append((key, value_type, _read_value_raw(handle, value_type)))
            tensors: list[_Tensor] = []
            for _ in range(tensor_count):
                name = _read_string(handle)
                dims_count = _read_u32(handle)
                if dims_count > 16:
                    raise GGUFMergeError(f"{path.name}: слишком много измерений тензора.")
                dims = tuple(_read_u64(handle) for _ in range(dims_count))
                ggml_type = _read_u32(handle)
                offset = _read_u64(handle)
                tensors.append(
                    _Tensor(name=name, dims=dims, ggml_type=ggml_type, offset=offset)
                )
            alignment = _alignment(kvs)
            data_start = _align_up(handle.tell(), alignment)
            if data_start > file_size:
                raise GGUFMergeError(f"{path.name}: заголовок больше файла.")
            return _Shard(
                path=path,
                version=version,
                kvs=kvs,
                tensors=tensors,
                data_start=data_start,
                file_size=file_size,
            )
    except OSError as exc:
        raise GGUFMergeError(f"Не удалось прочитать {path.name}: {exc}") from exc


def _shard_order(paths: list[Path]) -> list[Path]:
    indexed: list[tuple[int, Path]] = []
    counts: set[int] = set()
    prefixes: set[str] = set()
    for path in paths:
        part = split_part(path.name)
        if part is None:
            raise GGUFMergeError(f"{path.name}: это не часть разбитого GGUF.")
        indexed.append((part.index, path))
        counts.add(part.count)
        prefixes.add(part.prefix)
    if len(counts) != 1 or len(prefixes) != 1:
        raise GGUFMergeError("Части относятся к разным моделям.")
    count = next(iter(counts))
    indexed.sort(key=lambda item: item[0])
    if [index for index, _ in indexed] != list(range(1, count + 1)):
        raise GGUFMergeError(
            f"Не хватает частей: ожидалось {count}, найдено {len(indexed)}."
        )
    return [path for _, path in indexed]


def merge_gguf_shards(
    shards: list[Path],
    output: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Склеивает все шарды в один GGUF. Исходники не удаляет."""
    if output.is_file() and output.stat().st_size > 0:
        return output
    ordered = _shard_order(shards)
    parsed = [_parse_shard(path) for path in ordered]
    first = parsed[0]
    alignment = _alignment(first.kvs)

    for shard, path in zip(parsed, ordered, strict=True):
        part = split_part(path.name)
        if part is None:
            raise GGUFMergeError(f"{path.name}: неверное имя шарда.")
        number = _meta_int(shard.kvs, "split.no")
        total = _meta_int(shard.kvs, "split.count")
        if number is not None and number != part.index - 1:
            raise GGUFMergeError(f"{path.name}: split.no не совпадает с именем.")
        if total is not None and total != part.count:
            raise GGUFMergeError(f"{path.name}: split.count не совпадает с именем.")

    # Метаданные берём из первого шарда, убирая служебные split.*.
    metadata = [
        (key, value_type, raw)
        for key, value_type, raw in first.kvs
        if not key.startswith("split.")
    ]

    output_tensors: list[_Tensor] = []
    ranges: list[tuple[Path, int, int]] = []
    cursor = 0
    for shard in parsed:
        data_tensors = sorted(shard.tensors, key=lambda tensor: tensor.offset)
        for index, tensor in enumerate(data_tensors):
            end = (
                data_tensors[index + 1].offset
                if index + 1 < len(data_tensors)
                else shard.file_size - shard.data_start
            )
            if tensor.offset > end:
                raise GGUFMergeError(f"{shard.path.name}: тензоры пересекаются.")
            cursor = _align_up(cursor, alignment)
            placed = _Tensor(
                name=tensor.name,
                dims=tensor.dims,
                ggml_type=tensor.ggml_type,
                offset=tensor.offset,
                new_offset=cursor,
            )
            output_tensors.append(placed)
            ranges.append(
                (
                    shard.path,
                    shard.data_start + tensor.offset,
                    max(0, end - tensor.offset),
                )
            )
            cursor += max(0, end - tensor.offset)

    names = [tensor.name for tensor in output_tensors]
    if len(names) != len(set(names)):
        raise GGUFMergeError("В шардах повторяются имена тензоров.")
    expected_tensors = _meta_int(first.kvs, "split.tensors.count")
    if expected_tensors is not None and expected_tensors != len(output_tensors):
        raise GGUFMergeError(
            "Количество тензоров не совпадает с split.tensors.count: "
            f"{len(output_tensors)} вместо {expected_tensors}."
        )

    total = sum(size for _, _, size in ranges)
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        free = shutil.disk_usage(output.parent).free
    except OSError:
        free = 0
    if total and free and free < total + 64 * 1024 * 1024:
        raise GGUFMergeError(
            f"Недостаточно места для склейки: нужно {total / 1024**3:.1f} ГиБ, "
            f"свободно {free / 1024**3:.1f} ГиБ."
        )

    part_path = output.with_name(output.name + ".merge.part")
    part_path.unlink(missing_ok=True)
    done = 0
    try:
        with part_path.open("wb") as out:
            out.write(GGUF_MAGIC)
            _write_u32(out, first.version)
            _write_u64(out, len(output_tensors))
            _write_u64(out, len(metadata))
            for key, value_type, raw in metadata:
                _write_string(out, key)
                _write_u32(out, value_type)
                out.write(raw)
            for tensor in output_tensors:
                _write_string(out, tensor.name)
                _write_u32(out, len(tensor.dims))
                for dim in tensor.dims:
                    _write_u64(out, dim)
                _write_u32(out, tensor.ggml_type)
                _write_u64(out, tensor.new_offset)
            data_start = _align_up(out.tell(), alignment)
            out.write(b"\0" * (data_start - out.tell()))

            for index, (path, source_offset, size) in enumerate(ranges):
                target = data_start + output_tensors[index].new_offset
                if out.tell() < target:
                    out.write(b"\0" * (target - out.tell()))
                with path.open("rb") as source:
                    source.seek(source_offset)
                    remaining = size
                    while remaining > 0:
                        if cancel is not None and cancel.is_set():
                            raise GGUFMergeCancelled("Склейка отменена.")
                        chunk = source.read(min(COPY_CHUNK, remaining))
                        if not chunk:
                            raise GGUFMergeError(
                                f"{path.name}: файл оборван во время склейки."
                            )
                        out.write(chunk)
                        remaining -= len(chunk)
                        done += len(chunk)
                        if progress is not None:
                            progress(done, total)
            out.flush()
            os.fsync(out.fileno())
        os.replace(part_path, output)
    except GGUFMergeCancelled:
        part_path.unlink(missing_ok=True)
        raise
    except GGUFMergeError:
        part_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        part_path.unlink(missing_ok=True)
        raise GGUFMergeError(f"Не удалось склеить GGUF: {exc}") from exc

    if progress is not None:
        progress(total, total)
    return output


__all__ = [
    "GGUFMergeCancelled",
    "GGUFMergeError",
    "SplitPart",
    "is_split_gguf",
    "merge_gguf_shards",
    "merged_filename",
    "shard_filenames",
    "split_part",
]
