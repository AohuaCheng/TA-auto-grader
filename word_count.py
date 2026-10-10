"""Count essay body words (excluding title and references)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from docx import Document

MIN_WORDS = 800
MAX_WORDS = 1000

REF_HEADING = re.compile(
    r"^(references|bibliography|works cited|reference list|sources|citations)\s*:?\s*$",
    re.I,
)
TITLE_LIKE = re.compile(
    r"^(do plants have consciousness|psyche|essay|mind,? individual|"
    r"plants? consciousness|let there be light)",
    re.I,
)
WORD_PATTERN = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")


def count_words(text: str) -> int:
    return len(WORD_PATTERN.findall(text))


def count_essay_words(docx_path: Path) -> int:
    """Count words in essay body, excluding title and references."""
    doc = Document(str(docx_path))
    paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    if not paragraphs:
        return 0

    start = 0
    while start < len(paragraphs) and start < 3:
        text = paragraphs[start]
        if re.search(r"name:|学号|Part \d|Chinese name|English \(actually", text, re.I):
            start += 1
            continue
        if count_words(text) <= 12 and (
            TITLE_LIKE.search(text) or text == text.title() or "?" in text or len(text) < 80
        ):
            start += 1
            break
        if count_words(text) <= 8 and not text.endswith("."):
            start += 1
            continue
        break

    end = len(paragraphs)
    for index, text in enumerate(paragraphs):
        if REF_HEADING.match(text.strip()):
            end = index
            break

    while end > start and count_words(paragraphs[end - 1]) <= 3 and re.search(
        r"\d{10}|[A-Za-z]+\s+[A-Za-z]+", paragraphs[end - 1]
    ):
        end -= 1

    return count_words("\n".join(paragraphs[start:end]))


def resolve_submission_docx(student_dir: Path) -> Path | None:
    docxs = [
        p
        for p in student_dir.glob("*.docx")
        if "feedback" not in p.name.lower() and "firstround" not in p.name.lower()
    ]
    if not docxs:
        return None

    meta_path = student_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        for file_info in meta.get("files", []):
            filename = (file_info.get("filename") or "").strip()
            if not filename:
                continue
            stem = Path(filename).stem.lower()
            for docx in docxs:
                if docx.name.lower() == filename.lower() or docx.stem.lower() == stem:
                    return docx

    return max(docxs, key=lambda path: path.stat().st_size)


def word_count_remark(
    word_count: int,
    *,
    min_words: int = MIN_WORDS,
    max_words: int = MAX_WORDS,
    flag_under: bool = False,
) -> str | None:
    """Return a remark for out-of-range word counts.

    Under-count flags are off by default: our counter excludes punctuation and
    may read lower than Microsoft Word, which counts symbols toward the total.
    """
    if flag_under and word_count < min_words:
        return f"字数不足（{word_count}词，要求{min_words}-{max_words}）"
    if word_count > max_words:
        return f"字数超出（{word_count}词，要求{min_words}-{max_words}）"
    return None


UNDER_WORD_REMARK = re.compile(
    r"字数不足（\d+词，要求\d+-\d+）(；|$)"
)


def strip_under_word_remarks(remark: str | None) -> str | None:
    if not remark:
        return None
    cleaned = UNDER_WORD_REMARK.sub("", str(remark).strip())
    cleaned = re.sub(r"；+", "；", cleaned).strip("；").strip()
    return cleaned or None


def merge_remarks(existing: str | None, extra: str | None) -> str | None:
    parts: list[str] = []
    for item in (existing, extra):
        text = str(item).strip() if item else ""
        if text and text not in parts:
            parts.append(text)
    return "；".join(parts) if parts else None
