"""Upload ids and the S3 keys derived from them.

Ids are generated only on the server and keys are always built from a validated
id, so user input never chooses an S3 key.
"""

import secrets

UPLOAD_PREFIX = "uploads/"
RESULT_PREFIX = "results/"
UPLOAD_SUFFIX = ".txt"
RESULT_SUFFIX = ".json"
MAX_UPLOAD_BYTES = 100 * 1024

_ID_LENGTH = 32
_HEX = frozenset("0123456789abcdef")


def new_id() -> str:
    """128 random bits as 32 lowercase hex characters."""
    return secrets.token_hex(_ID_LENGTH // 2)


def is_valid_id(value: str) -> bool:
    return len(value) == _ID_LENGTH and all(ch in _HEX for ch in value)


def upload_key(upload_id: str) -> str:
    return f"{UPLOAD_PREFIX}{upload_id}{UPLOAD_SUFFIX}"


def result_key(upload_id: str) -> str:
    return f"{RESULT_PREFIX}{upload_id}{RESULT_SUFFIX}"


def id_from_upload_key(key: str) -> str | None:
    """Return the id in ``uploads/<id>.txt``, or None if the key has another shape."""
    if not (key.startswith(UPLOAD_PREFIX) and key.endswith(UPLOAD_SUFFIX)):
        return None
    candidate = key.removeprefix(UPLOAD_PREFIX).removesuffix(UPLOAD_SUFFIX)
    return candidate if is_valid_id(candidate) else None
