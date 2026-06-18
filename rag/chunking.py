"""
Split extracted text into overlapping chunks sized for embedding.

Recursive split on natural boundaries (paragraphs -> lines -> sentences ->
words) so chunks stay coherent, with a sliding overlap so facts that straddle
a boundary aren't lost.
"""
import re

from . import config

_MAX_CHARS = config.CHUNK_TOKENS * config.CHARS_PER_TOKEN
_OVERLAP_CHARS = config.CHUNK_OVERLAP_TOKENS * config.CHARS_PER_TOKEN

_SEPARATORS = ["\n\n", "\n", ". ", " "]


def _normalize(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_recursive(text, max_chars):
    """Greedily split text so no piece exceeds max_chars, preferring the
    earliest (most semantic) separator that helps."""
    if len(text) <= max_chars:
        return [text]
    for sep in _SEPARATORS:
        if sep not in text:
            continue
        parts, buf = [], ""
        for piece in text.split(sep):
            candidate = piece if not buf else buf + sep + piece
            if len(candidate) <= max_chars:
                buf = candidate
            else:
                if buf:
                    parts.append(buf)
                # A single piece can still be too big -> recurse on it.
                buf = piece if len(piece) <= max_chars else ""
                if len(piece) > max_chars:
                    parts.extend(_split_recursive(piece, max_chars))
        if buf:
            parts.append(buf)
        if parts:
            return parts
    # No separator helped (e.g. one long token): hard-slice.
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)]


def chunk_text(text, max_chars=_MAX_CHARS, overlap=_OVERLAP_CHARS):
    """Return a list of chunk strings with sliding overlap."""
    text = _normalize(text)
    if not text:
        return []
    base = _split_recursive(text, max_chars)
    if overlap <= 0 or len(base) <= 1:
        return base
    chunks = []
    for i, part in enumerate(base):
        if i == 0:
            chunks.append(part)
        else:
            tail = base[i - 1][-overlap:]
            chunks.append((tail + " " + part).strip())
    return chunks
