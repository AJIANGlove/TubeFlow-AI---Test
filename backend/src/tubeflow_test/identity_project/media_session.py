from __future__ import annotations

from typing import Literal

from fastapi import HTTPException, Response

from tubeflow_test.domain.errors import DomainError

from .domain import Account
from .service import IdentityProjectService

MEDIA_SESSION_COOKIE = "tubeflow_media_session"
MEDIA_SESSION_COOKIE_PATH = "/"
MEDIA_SESSION_SAME_SITE: Literal["lax"] = "lax"


def set_media_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    """Issue a browser-session cookie used only by explicit media dependencies."""
    response.set_cookie(
        MEDIA_SESSION_COOKIE,
        token,
        path=MEDIA_SESSION_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=MEDIA_SESSION_SAME_SITE,
    )


def clear_media_session_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        MEDIA_SESSION_COOKIE,
        path=MEDIA_SESSION_COOKIE_PATH,
        secure=secure,
        httponly=True,
        samesite=MEDIA_SESSION_SAME_SITE,
    )


def authenticate_media_account(
    service: IdentityProjectService,
    *,
    authorization: str | None,
    cookie_token: str | None,
    cookie_secure: bool,
) -> Account:
    """Authenticate media reads, preferring an explicitly supplied Bearer credential."""
    token: str | None = None
    used_cookie = False
    if authorization is not None:
        if authorization.startswith("Bearer "):
            token = authorization[7:]
    elif cookie_token:
        token = cookie_token
        used_cookie = True
    if not token:
        raise _invalid_session()
    try:
        return service.authenticate(token)
    except DomainError as error:
        if error.code != "invalid_session":
            raise
        headers = None
        if used_cookie:
            clearing = Response()
            clear_media_session_cookie(clearing, secure=cookie_secure)
            headers = {"Set-Cookie": clearing.headers["set-cookie"]}
        raise _invalid_session(headers) from error


def invalid_session_with_cookie_clear(*, secure: bool) -> HTTPException:
    clearing = Response()
    clear_media_session_cookie(clearing, secure=secure)
    return _invalid_session({"Set-Cookie": clearing.headers["set-cookie"]})


def _invalid_session(headers: dict[str, str] | None = None) -> HTTPException:
    return HTTPException(
        401,
        detail={"code": "invalid_session", "message": "Session is invalid"},
        headers=headers,
    )
