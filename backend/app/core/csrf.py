"""Origin guard for production API mutations authenticated by browser cookies."""

from fastapi import Request

from app.core.config import is_production_environment, resolve_cors_origins, settings


_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_SESSION_COOKIE = "novelcraft_session"


def csrf_request_allowed(request: Request) -> bool:
    """Use the configured allowlist, never a caller-controlled forwarded host.

    Native clients may omit Origin only when they do not send the session
    cookie or cross-site browser metadata. Cookie clients must supply Origin.
    """
    path = request.url.path
    if (
        not is_production_environment()
        or request.method in _SAFE_METHODS
        or not (path == "/api" or path.startswith("/api/"))
    ):
        return True

    origins = request.headers.getlist("origin")
    if origins:
        return (
            len(origins) == 1
            and origins[0] not in {"", "null", "*"}
            and origins[0] in resolve_cors_origins(settings.cors_allow_origins, debug=False)
        )
    return (
        _SESSION_COOKIE not in request.cookies
        and request.headers.get("sec-fetch-site", "").strip().lower() != "cross-site"
    )
