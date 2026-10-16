import ids
import pytest


def test_new_ids_are_valid_and_unique() -> None:
    generated = {ids.new_id() for _ in range(50)}
    assert len(generated) == 50
    assert all(ids.is_valid_id(value) for value in generated)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "abc",
        "0123456789ABCDEF0123456789ABCDEF",  # uppercase
        "0123456789abcdef0123456789abcdeg",  # non-hex
        "0123456789abcdef0123456789abcdef0",  # too long
        "../../0123456789abcdef0123456789",  # path tricks
        "0123456789abcdef0123456789abcde/",
        "0123456789abcdef0123456789abcdé",
        " 123456789abcdef0123456789abcdef",
    ],
)
def test_invalid_ids_are_rejected(value: str) -> None:
    assert not ids.is_valid_id(value)


def test_keys_are_built_from_the_id() -> None:
    assert ids.upload_key("a" * 32) == f"uploads/{'a' * 32}.txt"
    assert ids.result_key("a" * 32) == f"results/{'a' * 32}.json"


def test_id_round_trips_through_the_upload_key() -> None:
    upload_id = ids.new_id()
    assert ids.id_from_upload_key(ids.upload_key(upload_id)) == upload_id


@pytest.mark.parametrize(
    "key",
    [
        "results/" + "a" * 32 + ".json",
        "uploads/" + "a" * 32 + ".json",
        "uploads/nested/" + "a" * 32 + ".txt",
        "uploads/" + "A" * 32 + ".txt",
        "uploads/short.txt",
        "uploads/.txt",
        "other/" + "a" * 32 + ".txt",
        "",
    ],
)
def test_other_keys_are_not_uploads(key: str) -> None:
    assert ids.id_from_upload_key(key) is None
