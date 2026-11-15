"""Persistent, append-only article storage shared by collection runs."""

from .archive import ArticleArchive, archive_run

__all__ = ["ArticleArchive", "archive_run"]
