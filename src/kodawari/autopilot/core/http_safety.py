"""HTTP safety helpers shared by model transports."""

from __future__ import annotations

from typing import Any
from urllib import parse as urlparse
from urllib import request as urlrequest


class RedirectBlocked(RuntimeError):
    """Raised when an HTTP redirect would cross origin boundaries."""


def redirect_origin(url: str) -> str:
    parsed = urlparse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if not scheme or not host:
        return ""
    default_port = 443 if scheme == "https" else 80 if scheme == "http" else None
    port = parsed.port or default_port
    return f"{scheme}://{host}:{port or ''}"


class SafeRedirectHandler(urlrequest.HTTPRedirectHandler):
    """urllib redirect handler that blocks cross-origin auth leakage."""

    def redirect_request(
        self,
        req: urlrequest.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urlrequest.Request | None:
        old_origin = redirect_origin(req.full_url)
        new_origin = redirect_origin(newurl)
        if old_origin != new_origin:
            raise RedirectBlocked(f"redirect from {old_origin or '<unknown>'} to {new_origin or '<unknown>'}")
        if int(code or 0) in {307, 308}:
            return urlrequest.Request(
                newurl,
                data=req.data,
                headers=dict(req.headers),
                origin_req_host=req.origin_req_host,
                unverifiable=True,
                method=req.get_method(),
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


__all__ = ["RedirectBlocked", "SafeRedirectHandler", "redirect_origin"]
