"""Validation for CDK context values, so bad input fails at the boundary with a clear message."""

DEFAULT_RESERVED_CONCURRENCY = 2
MAX_RESERVED_CONCURRENCY = 100


def reserved_concurrency_from(value: object) -> int:
    """Validate the `reserved_concurrency` context value (-c reserved_concurrency=N).

    CLI context arrives as a string, JSON context as an int. 0 turns reservations off,
    for accounts whose Lambda quota is too small to reserve any.
    """
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise TypeError(f"reserved_concurrency must be an integer, got {value!r}")
    try:
        number = int(value)
    except ValueError:
        raise ValueError(
            f"reserved_concurrency must be an integer, got {value!r}"
        ) from None
    if not 0 <= number <= MAX_RESERVED_CONCURRENCY:
        raise ValueError(
            f"reserved_concurrency must be between 0 and {MAX_RESERVED_CONCURRENCY}, "
            f"got {number}"
        )
    return number
