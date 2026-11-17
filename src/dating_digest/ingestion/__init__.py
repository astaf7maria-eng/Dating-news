"""Shared extraction of saved HTML; network access stays with the session."""

from .pages import extract_article, extract_page, extract_provider_article, save_html_article

__all__ = ["extract_article", "extract_page", "extract_provider_article", "save_html_article"]
