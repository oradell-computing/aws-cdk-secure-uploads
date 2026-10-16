import pytest

from demo_serverless.config import MAX_RESERVED_CONCURRENCY, reserved_concurrency_from


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (2, 2), ("0", 0), ("2", 2), (" 7 ", 7), (MAX_RESERVED_CONCURRENCY, 100)],
)
def test_valid_values(value: object, expected: int) -> None:
    assert reserved_concurrency_from(value) == expected


@pytest.mark.parametrize(
    "value", ["two", "", "1.5", "-1", -1, MAX_RESERVED_CONCURRENCY + 1]
)
def test_bad_numbers_are_rejected_with_a_clear_message(value: object) -> None:
    with pytest.raises(ValueError, match="reserved_concurrency"):
        reserved_concurrency_from(value)


@pytest.mark.parametrize("value", [True, 1.5, None, [2], {"a": 1}])
def test_bad_types_are_rejected(value: object) -> None:
    with pytest.raises(TypeError, match="reserved_concurrency"):
        reserved_concurrency_from(value)
