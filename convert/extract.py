"""
extract.py — turn an unstructured source into clean prose text + metadata.

One small extractor per source type, plus a dispatcher that auto-detects:

  extract_text(path)  plain .txt / .md
  extract_html(html)  web page  -> main article text (trafilatura, bs4 fallback)
  extract_pdf(path)   scientific paper PDF -> text (pypdf)
  fetch(url)          download a URL and route by content-type
  extract(source)     auto: URL vs local file; .pdf/.html/.txt by extension/sniff

Each returns ``{"text": str, "meta": {...}}`` so the encoder is source-agnostic.
"""

from __future__ import annotations

import io
import os
import re

_WS = re.compile(r"[ \t ]+")
_NL = re.compile(r"\n{3,}")


def _tidy(text):
    text = text.replace("\r", "\n")
    text = _WS.sub(" ", text)
    text = _NL.sub("\n\n", text)
    return text.strip()


# ---------------------------------------------------------------- plain text
def extract_text(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return {"text": _tidy(fh.read()),
                "meta": {"type": "text", "source": path}}


# ---------------------------------------------------------------- HTML / web
def extract_html(html, url=None):
    """Main-content extraction. Prefer trafilatura; fall back to BeautifulSoup."""
    title = None
    text = None
    try:
        import trafilatura
        text = trafilatura.extract(html, include_comments=False,
                                   include_tables=False, favor_recall=True)
        md = trafilatura.extract_metadata(html)
        if md:
            title = md.title
    except Exception:
        text = None
    if not text:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
        for tag in soup(["script", "style", "nav", "header", "footer",
                         "aside", "noscript", "form"]):
            tag.decompose()
        if soup.title and soup.title.string:
            title = soup.title.string.strip()
        text = soup.get_text("\n")
    return {"text": _tidy(text or ""),
            "meta": {"type": "html", "source": url, "title": title}}


# ---------------------------------------------------------------- PDF / paper
def extract_pdf(path_or_bytes, source=None):
    from pypdf import PdfReader
    if isinstance(path_or_bytes, (bytes, bytearray)):
        reader = PdfReader(io.BytesIO(path_or_bytes))
        src = source
    else:
        reader = PdfReader(path_or_bytes)
        src = source or path_or_bytes
    pages = []
    for pg in reader.pages:
        try:
            pages.append(pg.extract_text() or "")
        except Exception:
            pages.append("")
    title = None
    try:
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title)
    except Exception:
        pass
    return {"text": _tidy("\n\n".join(pages)),
            "meta": {"type": "pdf", "source": src, "title": title,
                     "pages": len(pages)}}


# ---------------------------------------------------------------- URL fetch
def fetch(url, timeout=40):
    import requests
    headers = {"User-Agent": "Mozilla/5.0 (prose-to-lexicon/1.0)"}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "").lower()
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        return extract_pdf(r.content, source=url)
    return extract_html(r.text, url=url)


# ---------------------------------------------------------------- dispatcher
def extract(source, kind="auto"):
    """Route a source string (URL or path) to the right extractor."""
    if kind == "url" or (kind == "auto" and re.match(r"^https?://", source)):
        return fetch(source)
    if kind == "pdf" or (kind == "auto" and source.lower().endswith(".pdf")):
        return extract_pdf(source)
    if kind == "html" or (kind == "auto" and
                          source.lower().endswith((".html", ".htm"))):
        with open(source, encoding="utf-8", errors="replace") as fh:
            return extract_html(fh.read(), url=source)
    return extract_text(source)
