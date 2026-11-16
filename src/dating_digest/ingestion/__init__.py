"""Shared extraction of saved HTML; network access stays with the session."""

from .pages import extract_article, extract_page

__all__ = ["extract_article", "extract_page"]
