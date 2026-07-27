from __future__ import annotations

import os
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAX_FILENAME_LENGTH = 120
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
INVALID_COMPONENT_CHARS = re.compile(r'[\\/*?:"<>|\x00-\x1f]')


def sanitize_component(value: object, *, fallback: str, max_length: int = 175) -> str:
    """Return one safe Windows-compatible path component."""
    component = INVALID_COMPONENT_CHARS.sub("", str(value or "")).strip().rstrip(". ")
    if component in {"", ".", ".."}:
        component = fallback

    stem = component.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        component = f"_{component}"

    if len(component) > max_length:
        tail_length = min(72, max_length // 2)
        head_length = max_length - tail_length - 3
        component = f"{component[:head_length].rstrip()}...{component[-tail_length:]}"
    return component or fallback


def sanitize_filename(
    value: object,
    *,
    fallback: str = "download.bin",
    max_length: int = MAX_FILENAME_LENGTH,
) -> str:
    raw = sanitize_component(value, fallback=fallback, max_length=max_length * 2)
    suffix = Path(raw).suffix
    if len(raw) <= max_length:
        return raw

    suffix = suffix[:16]
    stem_budget = max(1, max_length - len(suffix))
    return f"{Path(raw).stem[:stem_budget]}{suffix}"


def unique_filename(
    value: str, taken: set[str], *, max_length: int = MAX_FILENAME_LENGTH
) -> str:
    """Return *value*, or a numbered variant of it that is not already *taken*.

    Names are compared case-insensitively because Windows paths are. The
    counter is appended after truncating the stem, so two long names that
    sanitize to the same head still resolve to distinct files instead of
    sharing one destination.
    """
    if value.lower() not in taken:
        return value

    path = Path(value)
    stem = path.stem
    suffix = path.suffix[:16]
    counter = 2
    while True:
        marker = f"_{counter}"
        stem_budget = max(1, max_length - len(suffix) - len(marker))
        head = stem[:stem_budget].rstrip() or "file"
        candidate = f"{head}{marker}{suffix}"
        if candidate.lower() not in taken:
            return candidate
        counter += 1


def resolve_within(base: str | os.PathLike[str], *parts: str) -> Path:
    """Resolve a child path and reject traversal outside *base*."""
    base_path = Path(base).expanduser().resolve(strict=False)
    candidate = base_path.joinpath(*parts).resolve(strict=False)
    try:
        candidate.relative_to(base_path)
    except ValueError as exc:
        raise ValueError(f"Path escapes download directory: {candidate}") from exc
    return candidate


def task_output_path(base_dir: str, title: object, *, flat_directory: bool = False) -> Path:
    base = Path(base_dir).expanduser().resolve(strict=False)
    if flat_directory:
        return base
    return resolve_within(base, sanitize_component(title, fallback="Unknown Album"))
