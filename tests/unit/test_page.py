from html.parser import HTMLParser
from pathlib import Path

import page
import pytest

from .helpers import UPLOAD_ORIGIN

STATIC = Path(page.__file__).parent / "static"


def get(path: str) -> dict:
    return page.handler({"rawPath": path}, None)


@pytest.mark.parametrize(
    ("path", "filename", "content_type"),
    [
        ("/", "index.html", "text/html"),
        ("/app.js", "app.js", "text/javascript"),
        ("/app.css", "app.css", "text/css"),
    ],
)
def test_serves_each_file_with_its_content_type(
    path: str, filename: str, content_type: str
) -> None:
    response = get(path)
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"].startswith(content_type)
    assert response["body"] == (STATIC / filename).read_text()


@pytest.mark.parametrize(
    "path", ["/other", "/../page.py", "/static/app.js", "/app.js/"]
)
def test_unknown_paths_are_404(path: str) -> None:
    assert get(path)["statusCode"] == 404


def test_every_response_has_the_security_headers() -> None:
    for path in ("/", "/app.js", "/app.css", "/missing"):
        headers = get(path)["headers"]
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer"
        assert headers["Strict-Transport-Security"].startswith("max-age=")
        assert headers["X-Frame-Options"] == "DENY"
        assert "Content-Security-Policy" in headers


def test_csp_is_strict_and_allows_only_our_bucket_for_uploads() -> None:
    csp = get("/")["headers"]["Content-Security-Policy"]
    directives = dict(part.split(" ", 1) for part in csp.split("; "))
    assert "unsafe-inline" not in csp
    assert "unsafe-eval" not in csp
    assert directives["default-src"] == "'none'"
    assert directives["script-src"] == "'self'"
    assert directives["style-src"] == "'self'"
    assert directives["connect-src"] == f"'self' {UPLOAD_ORIGIN}"
    assert directives["form-action"] == "'none'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["base-uri"] == "'none'"


class _Inspector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.problems: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name for name, _ in attrs}
        values = dict(attrs)
        if tag == "script" and "src" not in names:
            self.problems.append("inline <script>")
        if tag == "style":
            self.problems.append("inline <style>")
        if "style" in names:
            self.problems.append("style attribute")
        self.problems += [
            f"event handler {name}" for name in names if name.startswith("on")
        ]
        for url in (values.get("src"), values.get("href")):
            if url and not url.startswith("/"):
                self.problems.append(f"external reference {url}")


def test_html_has_no_inline_code_or_external_references() -> None:
    inspector = _Inspector()
    inspector.feed((STATIC / "index.html").read_text())
    assert inspector.problems == []


@pytest.mark.parametrize(
    "forbidden",
    [
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
    ],
)
def test_script_never_writes_markup(forbidden: str) -> None:
    assert forbidden not in (STATIC / "app.js").read_text()


def test_script_shows_results_with_textcontent() -> None:
    assert "textContent" in (STATIC / "app.js").read_text()


# The browser script can't be run here (no JS tooling by design), so these pin the
# behaviour that matters in its source. The real check is the browser step at deploy time.


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    return source[start : source.index("\n}\n", start)]


def test_script_clears_the_previous_result_before_anything_else_in_run() -> None:
    body = _function_body((STATIC / "app.js").read_text(), "async function run() {")
    statements = [line.strip() for line in body.splitlines()[1:] if line.strip()]
    assert statements[0].startswith("clearResult();")


def test_script_clears_the_result_when_processing_fails() -> None:
    body = _function_body((STATIC / "app.js").read_text(), "async function run() {")
    catch_block = body[body.index("} catch (error) {") :]
    assert "clearResult();" in catch_block
    error_result_branch = body[body.index("} else {") : body.index("} catch")]
    assert "clearResult();" in error_result_branch


def test_script_keeps_polling_while_the_result_is_pending() -> None:
    source = (STATIC / "app.js").read_text()
    assert 'result.status !== "pending"' in source


def test_every_value_shown_goes_through_the_textcontent_helper() -> None:
    source = (STATIC / "app.js").read_text()
    assert "node.textContent = text" in source
    assert "createElement" in source
    assert 'id="output"' in (STATIC / "index.html").read_text()
