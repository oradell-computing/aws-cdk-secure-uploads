"""HTTP API (payload v2) responses with the security headers on every one."""

import json
from typing import Any

import aws

STATIC_CACHE = "public, max-age=300"
NO_STORE = "no-store"

_SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def content_security_policy() -> str:
    """No 'unsafe-inline': script and CSS are files served from this origin."""
    directives = [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        f"connect-src 'self' {aws.upload_origin()}",
        "img-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
    return "; ".join(directives)


def build(
    status: int, body: str, content_type: str, cache_control: str = NO_STORE
) -> dict[str, Any]:
    headers = {
        **_SECURITY_HEADERS,
        "Content-Type": content_type,
        "Cache-Control": cache_control,
        "Content-Security-Policy": content_security_policy(),
    }
    return {"statusCode": status, "headers": headers, "body": body}


def json_response(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return build(status, json.dumps(payload), "application/json; charset=utf-8")
