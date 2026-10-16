"""The fingerprint is a pure function, so these tests need no AWS, fixtures or fakes."""

import hashlib

import fingerprint
import pytest
from fingerprint import InvalidTextError
from fingerprint import fingerprint as fingerprint_of


def words(text: str) -> list[str]:
    return fingerprint.words_in(text)


# --- hash and sizes ---------------------------------------------------------------------


def test_sha256_is_of_the_raw_bytes_like_sha256sum() -> None:
    data = b"hello world\n"
    assert fingerprint_of(data)["sha256"] == hashlib.sha256(data).hexdigest()
    assert (
        fingerprint_of(data)["sha256"]
        == "a948904f2f0f479b8f8197694b30184b0d2ed1c1cd2a1ec0fb85d299a192a447"
    )


def test_the_hash_includes_a_bom_but_the_text_does_not() -> None:
    data = b"\xef\xbb\xbfhello"
    result = fingerprint_of(data)
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["bytes"] == 8
    assert result["top_words"] == [{"word": "hello", "count": 1}]


def test_same_bytes_give_the_same_result() -> None:
    data = b"The cat. The dog! The end?"
    assert fingerprint_of(data) == fingerprint_of(data)


def test_full_result_for_a_small_text() -> None:
    result = fingerprint_of(b"Hello, fingerprint. Hello again.\n")
    assert result == {
        "sha256": hashlib.sha256(b"Hello, fingerprint. Hello again.\n").hexdigest(),
        "bytes": 33,
        "word_count": 4,
        "unique_words": 3,
        "top_words": [
            {"word": "hello", "count": 2},
            {"word": "again", "count": 1},
            {"word": "fingerprint", "count": 1},
        ],
        "sentence_count": 2,
        "avg_sentence_length": 2.0,
        "reading_time_seconds": 1,
    }


# --- words ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello, World!", ["hello", "world"]),
        ("don't stop", ["don't", "stop"]),
        ("don’t stop", ["don't", "stop"]),  # typographic apostrophe is the same word
        ("'quoted' words", ["quoted", "words"]),
        ("rock'n'roll", ["rock'n'roll"]),
        ("trailing' x", ["trailing", "x"]),
        ("a--b c_d", ["a", "b", "c", "d"]),
        ("route 66 and 3.14", ["route", "66", "and", "3", "14"]),
        ("ÉCOLE école", ["école", "école"]),
        ("straße STRASSE", ["strasse", "strasse"]),  # casefold, not lower
        ("école", ["école"]),  # decomposed accent is normalized (NFC)
        ("İstanbul", ["i̇stanbul"]),  # a mark created by casefold stays in its word
        ("日本語 テキスト", ["日本語", "テキスト"]),
        ("", []),
        ("... --- !!!", []),
    ],
)
def test_word_splitting(text: str, expected: list[str]) -> None:
    assert words(text) == expected


def test_word_and_unique_counts() -> None:
    result = fingerprint_of(b"one Two two THREE three three")
    assert result["word_count"] == 6
    assert result["unique_words"] == 3


# --- top words --------------------------------------------------------------------------


def test_top_words_are_ordered_by_count_then_alphabetically() -> None:
    result = fingerprint_of(b"pear apple pear banana apple cherry")
    assert result["top_words"] == [
        {"word": "apple", "count": 2},
        {"word": "pear", "count": 2},
        {"word": "banana", "count": 1},
        {"word": "cherry", "count": 1},
    ]


def test_top_words_are_limited_to_ten() -> None:
    text = " ".join(f"w{number:02d}" for number in range(25))
    top = fingerprint_of(text.encode())["top_words"]
    assert [entry["word"] for entry in top] == [f"w{n:02d}" for n in range(10)]


def test_stop_words_are_counted_like_any_other_word() -> None:
    assert fingerprint_of(b"the the the cat")["top_words"][0] == {
        "word": "the",
        "count": 3,
    }


# --- sentences --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("One. Two. Three.", 3),
        ("Hi... there!", 2),  # a run of terminators ends one sentence
        ("What?! Really?!", 2),
        ("No terminator at all", 1),
        ("Ends. And trailing words", 2),
        ("Pi is 3.14 today.", 1),  # a decimal point is not a sentence end
        ("Version 1. Next.", 2),  # but a period after a digit followed by a space is
        ("...Start. End.", 2),  # terminators before any word are ignored
        ("Line one.\n\nLine two.\n", 2),
        ("", 0),
        ("!!!", 0),
    ],
)
def test_sentence_counting(text: str, expected: int) -> None:
    assert fingerprint.sentence_count(text) == expected


def test_average_sentence_length_is_rounded_to_one_decimal() -> None:
    # 7 words in 3 sentences
    assert fingerprint_of(b"One two. Three four five. Six seven.")[
        "avg_sentence_length"
    ] == pytest.approx(2.3)


def test_abbreviations_over_count_sentences() -> None:
    """Documented limitation: no abbreviation list."""
    # "See e." / "g." / "this one." is 3 sentences where a reader sees 1.
    assert fingerprint_of(b"See e.g. this one.")["sentence_count"] == 3


# --- reading time -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("word_count", "seconds"),
    [(1, 0), (2, 1), (4, 1), (5, 2), (200, 60), (201, 60), (500, 150), (1000, 300)],
)
def test_reading_time_is_200_words_per_minute_rounded_half_up(
    word_count: int, seconds: int
) -> None:
    data = " ".join(["word"] * word_count).encode()
    assert fingerprint_of(data)["reading_time_seconds"] == seconds


# --- rejected input -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"\xff\xfe not utf-8 \x80", "not valid UTF-8"),
        (b"caf\xe9", "not valid UTF-8"),  # latin-1, not UTF-8
        (b"\xff\xd8\xff\xe0\x00\x10JFIF", "not valid UTF-8"),  # a JPEG header
        (b"text with a \x00 NUL byte", "binary data"),
        (b"", "No words"),
        (b"\xef\xbb\xbf", "No words"),  # a BOM and nothing else
        (b"   \n\t  \n", "No words"),
        (b"... !!! ???", "No words"),
    ],
)
def test_input_that_is_not_usable_text_is_rejected(data: bytes, message: str) -> None:
    with pytest.raises(InvalidTextError, match=message):
        fingerprint_of(data)


def test_invalid_text_error_is_a_value_error() -> None:
    assert issubclass(InvalidTextError, ValueError)


# --- hostile input -------------------------------------------------------------------------


def test_large_inputs_with_pathological_shapes_finish_quickly() -> None:
    """Plain loops, no regex: nothing here can backtrack."""
    size = 100 * 1024
    for data in (
        b"a" * size,
        b"'" * size,
        b"a'" * (size // 2),
        b". " * (size // 2),
        ("é" * (size // 2)).encode(),
        b"x " * (size // 2),
    ):
        try:
            fingerprint_of(data)
        except InvalidTextError:
            pass  # e.g. only apostrophes: rejected, which is fine


def test_markup_in_the_text_is_just_words() -> None:
    result = fingerprint_of(b"<script>alert(1)</script>")
    assert [entry["word"] for entry in result["top_words"]] == [
        "script",  # twice, so first
        "1",
        "alert",
    ]
