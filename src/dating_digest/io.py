"""UTF-8 JSON I/O; immutable publication by default, explicit mutable reports."""

import json
import os
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


@contextmanager
def _atomic_output(path: Path, replace: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".write-", dir=path.parent) as temporary:
        staging = Path(temporary) / path.name
        with staging.open("x", encoding="utf-8", newline="\n") as output:
            yield output
            output.flush()
            os.fsync(output.fileno())
        if replace:
            os.replace(staging, path)
        else:
            os.link(staging, path)


def write_text(path: Path, value: str, *, replace: bool = False) -> None:
    """Publish UTF-8 text with the same atomic replacement rules as JSON."""
    with _atomic_output(path, replace) as output:
        output.write(value)


def write_json(path: Path, value: dict | list, *, replace: bool = False) -> None:
    with _atomic_output(path, replace) as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
