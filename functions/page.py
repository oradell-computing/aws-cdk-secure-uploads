"""GET /, /app.js, /app.css: serves the static page files bundled with the function."""

from pathlib import Path
from typing import Any

import responses

_STATIC = Path(__file__).parent / "static"

# Fixed map from request path to (file, content type): no user input picks a file.
_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    entry = _FILES.get(event["rawPath"])
    if entry is None:
        return responses.json_response(404, {"error": "not found"})
    filename, content_type = entry
    body = (_STATIC / filename).read_text(encoding="utf-8")
    return responses.build(200, body, content_type, responses.STATIC_CACHE)
