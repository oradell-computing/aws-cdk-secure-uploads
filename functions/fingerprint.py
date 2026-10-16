"""The text fingerprint: a pure function from bytes to a dict. No AWS, no I/O, no clock.

Same bytes in, same dict out, so a repeated S3 event rewrites an identical result.
Words and sentences are found with plain character loops, not regular expressions, so
hostile input can't cause catastrophic backtracking.
"""

import hashlib
import unicodedata
from collections import Counter
from typing import Any

TOP_WORD_COUNT = 10
WORDS_PER_MINUTE = 200

_APOSTROPHES = "'’"  # straight and typographic
_SENTENCE_ENDS = ".!?"


class InvalidTextError(ValueError):
    """The upload isn't usable text. Expected bad input, shown to the visitor as a message."""


def decode(data: bytes) -> str:
    """Strict UTF-8 (a leading BOM is dropped); binary data is rejected."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise InvalidTextError("The file is not valid UTF-8 text.") from None
    if "\x00" in text:
        raise InvalidTextError("The file contains binary data, not text.")
    return text


def words_in(text: str) -> list[str]:
    """Lower-cased words: runs of letters and digits, with an apostrophe kept only
    between two of them (``don't`` is one word, ``'quoted'`` is ``quoted``)."""
    text = unicodedata.normalize("NFC", text).casefold()  # NFC keeps accents attached
    words: list[str] = []
    current: list[str] = []
    for index, char in enumerate(text):
        is_inner_apostrophe = (
            char in _APOSTROPHES
            and bool(current)
            and index + 1 < len(text)
            and text[index + 1].isalnum()
        )
        # Combining marks stay with their word (casefold can create them: İ -> i + U+0307).
        is_mark = bool(current) and unicodedata.category(char).startswith("M")
        if char.isalnum() or is_mark:
            current.append(char)
        elif is_inner_apostrophe:
            current.append("'")  # one spelling, so don't and don’t count together
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return words


def sentence_count(text: str) -> int:
    """A sentence ends at ``.``, ``!`` or ``?`` after at least one word; trailing words
    with no terminator count as one more. A ``.`` between digits (3.14) is not an end.
    Abbreviations ("e.g.") still over-count; that is accepted for a demo."""
    count = 0
    has_words = False
    for index, char in enumerate(text):
        if char.isalnum():
            has_words = True
        elif char in _SENTENCE_ENDS and has_words:
            is_decimal_point = (
                char == "."
                and 0 < index < len(text) - 1
                and text[index - 1].isdigit()
                and text[index + 1].isdigit()
            )
            if not is_decimal_point:
                count += 1
                has_words = False
    return count + 1 if has_words else count


def top_words(words: list[str]) -> list[dict[str, Any]]:
    """Most frequent first; ties broken alphabetically so the order is deterministic."""
    ranked = sorted(Counter(words).items(), key=lambda item: (-item[1], item[0]))
    return [{"word": word, "count": count} for word, count in ranked[:TOP_WORD_COUNT]]


def fingerprint(data: bytes) -> dict[str, Any]:
    """Fingerprint an uploaded file. Raises InvalidTextError for input that isn't text
    or has no words in it."""
    text = decode(data)
    words = words_in(text)
    if not words:
        raise InvalidTextError("No words were found in the file.")
    sentences = sentence_count(text)
    return {
        "sha256": hashlib.sha256(data).hexdigest(),  # of the raw bytes, like sha256sum
        "bytes": len(data),
        "word_count": len(words),
        "unique_words": len(set(words)),
        "top_words": top_words(words),
        "sentence_count": sentences,
        "avg_sentence_length": round(len(words) / sentences, 1),
        # Integer arithmetic so halves always round up, whatever the platform.
        "reading_time_seconds": (len(words) * 60 + WORDS_PER_MINUTE // 2)
        // WORDS_PER_MINUTE,
    }
