"""Sign in, sign out, and session introspection.

Sessions are rows in the database (`sessions.py`), so logout here revokes
rather than just clearing the browser's copy. Finding 4 of the 2026-10-29
security review.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import security, sessions

log = logging.getLogger("nightwire.auth")
router = APIRouter()


class LoginBody(BaseModel):
    password: str = ""


def _client(request: Request) -> str:
    # The socket's peer address, deliberately not X-Forwarded-For: the
    # throttle below keys on this, and a client-supplied header would let one
    # attacker present a new identity per attempt. uvicorn is started without
    # --proxy-headers so this stays the real peer (see entrypoint.sh).
    return request.client.host if request.client else "unknown"


def _conn(request: Request) -> sqlite3.Connection | None:
    """The app's database handle, or None before the lifespan has opened it."""
    return getattr(request.app.state, "db", None)


def _fingerprint(request: Request) -> str:
    settings = request.app.state.settings
    return security.password_fingerprint(settings.secret_key, settings.password)


def _set_cookie(response: JSONResponse, request: Request, token: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        security.COOKIE_NAME,
        token,
        max_age=settings.session_days * 86400,
        httponly=True,
        samesite="lax",
        # No `secure`: this is a LAN-only app served over plain HTTP, and a
        # secure cookie would never be sent back. See .
        secure=False,
        path="/",
    )


@router.post("/auth/login")
async def login(body: LoginBody, request: Request) -> JSONResponse:
    settings = request.app.state.settings
    limiter = request.app.state.login_limiter
    client = _client(request)

    decision = limiter.check(client)
    if not decision.allowed:
        log.warning("login blocked for %s (%s), retry in %ss", client, decision.reason, decision.retry_after)
        return JSONResponse(
            {"error": f"Too many attempts ({decision.reason}). Try again in {decision.retry_after}s."},
            status_code=429,
            headers={"Retry-After": str(decision.retry_after)},
        )

    # Fixed cost on every attempt, successful or not.
    await asyncio.sleep(limiter.min_delay_seconds)

    if not security.check_password(settings.password, body.password):
        after = limiter.record_failure(client)
        log.warning("failed login from %s (%d failures)", client, limiter.failures(client))
        payload = {"error": "Incorrect password."}
        headers = {}
        if not after.allowed:
            payload["error"] = f"Incorrect password. Locked for {after.retry_after}s."
            headers["Retry-After"] = str(after.retry_after)
        return JSONResponse(payload, status_code=401, headers=headers)

    connection = _conn(request)
    if connection is None:  # pragma: no cover - the lifespan always opens it
        log.error("no database handle: cannot issue a session")
        return JSONResponse({"error": "The server is still starting."}, status_code=503)

    limiter.record_success(client)
    log.info("successful login from %s", client)
    token = sessions.create(
        connection,
        password_fp=_fingerprint(request),
        ttl_seconds=settings.session_days * 86400,
        client=client,
    )
    response = JSONResponse({"ok": True})
    _set_cookie(response, request, token)
    return response


@router.post("/auth/logout")
def logout(request: Request) -> JSONResponse:
    """Sign out. Revokes the session rather than only clearing the cookie.

    Public, like login: a request with no valid session still gets an ok and
    a cleared cookie, because telling an unauthenticated caller whether a
    token was real is free information for no benefit.
    """
    connection = _conn(request)
    token = request.cookies.get(security.COOKIE_NAME)
    if connection is not None and sessions.revoke(connection, token):
        log.info("session revoked by logout from %s", _client(request))
    response = JSONResponse({"ok": True})
    response.delete_cookie(security.COOKIE_NAME, path="/")
    return response


@router.get("/auth/session")
def session(request: Request) -> JSONResponse:
    """Whether this browser is signed in. Public, so the login page can ask."""
    settings = request.app.state.settings
    connection = _conn(request)
    token = request.cookies.get(security.COOKIE_NAME)
    row = (
        None
        if connection is None
        else sessions.verify(
            connection,
            token,
            password_fp=_fingerprint(request),
            ttl_seconds=settings.session_days * 86400,
        )
    )
    if row is None:
        return JSONResponse({"authenticated": False}, status_code=200)

    created_ms = row["created_ms"]
    response = JSONResponse(
        {"authenticated": True, "issued_at": created_ms // 1000}
    )
    # The server-side expiry slides on use, but the cookie's own max-age was
    # fixed when it was set, so a tab left open for weeks needs it re-sent.
    # The same token: rotating it here would sign out every other station tab
    # that polled this endpoint a moment later.
    if security.needs_refresh(created_ms, settings.session_refresh_days * 86400):
        _set_cookie(response, request, token or "")
    return response
