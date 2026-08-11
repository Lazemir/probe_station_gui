"""Pure source-path identity helpers for GUI-thread coordinate policy."""

from __future__ import annotations

import os


def source_identity(value: str | os.PathLike[str]) -> str:
    """Return a stable absolute path string without touching the filesystem."""

    path = os.path.expanduser(os.fspath(value))
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = ["source_identity"]
