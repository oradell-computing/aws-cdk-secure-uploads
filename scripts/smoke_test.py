"""Post-deploy smoke test for the text-fingerprint demo (standard library only).

Normally run through the Makefile: `make smoke`. To run it by hand, use the project's
interpreter: .venv/bin/python scripts/smoke_test.py <SiteUrl>

SiteUrl is the stack's `SiteUrl` output. Exits non-zero if any check fails.
"""

import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from email.message import Message as Headers
from typing import Any

POLL_SECONDS = 30
MAX_BYTES = 100 * 1024
SAMPLE = b"Hello, fingerprint. Hello again.\n"  # 4 words, 3 unique, "hello" twice

failures: list[str] = []


def check(name: str, ok: bool, detail: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        failures.append(name)


def request(
    url: str,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Headers, bytes]:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:  # 4xx/5xx are results to check, not crashes
        return error.code, error.headers, error.read()


def multipart(fields: dict[str, str], content: bytes) -> tuple[bytes, str]:
    """A multipart/form-data body with the file as the last part, as S3 requires."""
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="sample.txt"\r\nContent-Type: text/plain\r\n\r\n'.encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def new_upload(site: str) -> dict[str, Any]:
    time.sleep(1.1)  # POST /uploads is throttled to 1 request per second
    status, _, body = request(f"{site}/uploads", method="POST")
    if status != 200:
        raise SystemExit(f"POST /uploads returned {status}: {body[:200]!r}")
    return json.loads(body)


def upload(
    site: str, link: dict[str, Any], content: bytes, **overrides: str
) -> tuple[int, Headers]:
    fields = {**link["fields"], **overrides}
    body, content_type = multipart(fields, content)
    status, headers, _ = request(
        link["url"], "POST", body, {"Content-Type": content_type, "Origin": site}
    )
    return status, headers


def wait_for_result(site: str, upload_id: str) -> tuple[int, Headers, dict[str, Any]]:
    """Poll until the result is no longer pending (or POLL_SECONDS pass)."""
    deadline = time.monotonic() + POLL_SECONDS
    while True:
        status, headers, body = request(f"{site}/results/{upload_id}")
        result: dict[str, Any] = json.loads(body) if status == 200 else {}
        if status != 200 or result.get("status") != "pending":
            return status, headers, result
        if time.monotonic() >= deadline:
            return status, headers, result
        time.sleep(1)


def main(site: str) -> int:
    if not site.startswith("https://"):
        raise SystemExit("SiteUrl must start with https://")
    site = site.rstrip("/")

    status, headers, body = request(f"{site}/")
    csp = headers.get("Content-Security-Policy", "")
    check("page loads", status == 200 and b"<html" in body, status)
    check(
        "page has a strict CSP",
        "unsafe-inline" not in csp and "script-src 'self'" in csp,
        csp,
    )
    check("page has nosniff", headers.get("X-Content-Type-Options") == "nosniff")
    check("app.js loads", request(f"{site}/app.js")[0] == 200)

    link = new_upload(site)
    # The CSP origin (from the bucket's regional domain name) and the presigned URL
    # (from the S3 client's endpoint) come from different places; they must agree.
    parts = urllib.parse.urlsplit(link["url"])
    check(
        "CSP connect-src allows the presigned upload URL's origin",
        f"{parts.scheme}://{parts.netloc}" in csp,
        f"{link['url']} vs {csp}",
    )
    status, headers = upload(site, link, SAMPLE)
    check("direct upload to S3 succeeds (204)", status == 204, status)
    check(
        "S3 allows the page's origin (CORS)",
        headers.get("Access-Control-Allow-Origin") == site,
        headers.get("Access-Control-Allow-Origin"),
    )

    status, headers, result = wait_for_result(site, link["id"])
    check("result arrives through the API (200)", status == 200, status)
    check("result is ok", result.get("status") == "ok", result)
    fingerprint = result.get("fingerprint", {})
    check(
        "SHA-256 matches the uploaded bytes",
        fingerprint.get("sha256") == hashlib.sha256(SAMPLE).hexdigest(),
        fingerprint.get("sha256"),
    )
    check(
        "word counts and top word are right",
        (fingerprint.get("word_count"), fingerprint.get("unique_words")) == (4, 3)
        and fingerprint.get("top_words", [{}])[0] == {"word": "hello", "count": 2},
        fingerprint,
    )
    check("results are not cacheable", headers.get("Cache-Control") == "no-store")

    check("bad id is rejected (400)", request(f"{site}/results/not-an-id")[0] == 400)
    status, headers, body = request(f"{site}/results/{'0' * 32}")
    check(
        "a result that isn't there yet is 200 pending",
        status == 200 and json.loads(body) == {"status": "pending"},
        (status, body),
    )
    check("pending is not cacheable", headers.get("Cache-Control") == "no-store")

    bad_link = new_upload(site)
    upload(site, bad_link, b"caf\xe9 is latin-1, not UTF-8")
    status, _, result = wait_for_result(site, bad_link["id"])
    check(
        "text that isn't UTF-8 gets a readable error result",
        status == 200
        and result.get("status") == "error"
        and "UTF-8" in result.get("error", ""),
        result,
    )

    status, _ = upload(site, new_upload(site), b"x" * (MAX_BYTES + 1))
    check("oversized upload is rejected by S3", status in (400, 403), status)
    status, _ = upload(
        site, new_upload(site), b"<b>hi</b>", **{"Content-Type": "text/html"}
    )
    check("wrong content type is rejected by S3", status in (400, 403), status)

    status, _, _ = request(f"{link['url']}results/{link['id']}.json")
    check("bucket objects are not publicly readable", status == 403, status)

    print(f"\n{len(failures)} failure(s)" if failures else "\nAll checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    sys.exit(main(sys.argv[1]))
