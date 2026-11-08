"""Guest sessions and the request dependencies that resolve the caller.

Clients send ``Authorization: Bearer <token>``. Browser features that cannot
set headers (EventSource, ``<img>`` for MJPEG) may pass ``?access_token=``.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Annotated

from fastapi import Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from cloudbench.gateway.db import Principal, PrincipalKind, SessionToken
from cloudbench.gateway.errors import ApiError
from cloudbench.gateway.services import Services, get_services
from cloudbench.gateway.util import new_id, utcnow


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _extract_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value.strip()
    return request.query_params.get("access_token") or None


class SessionCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=40)


class PrincipalOut(BaseModel):
    id: str
    display_name: str
    kind: str


class SessionOut(BaseModel):
    token: str
    principal: PrincipalOut


def principal_out(p: Principal) -> PrincipalOut:
    return PrincipalOut(id=p.id, display_name=p.display_name, kind=p.kind)


async def create_session(services: Services, display_name: str) -> SessionOut:
    token = "cbt_" + secrets.token_urlsafe(32)
    now = utcnow()
    principal = Principal(
        id=new_id("usr"),
        kind=PrincipalKind.GUEST,
        display_name=display_name.strip(),
        created_at=now,
    )
    async with services.db.session() as s:
        s.add(principal)
        s.add(
            SessionToken(token_hash=_hash_token(token), principal_id=principal.id, created_at=now)
        )
        await s.commit()
    services.presence.touch(principal.id)
    return SessionOut(token=token, principal=principal_out(principal))


async def optional_principal(
    request: Request, services: Annotated[Services, Depends(get_services)]
) -> Principal | None:
    token = _extract_token(request)
    if token is None:
        return None
    async with services.db.session() as s:
        row = await s.execute(
            select(Principal)
            .join(SessionToken, SessionToken.principal_id == Principal.id)
            .where(SessionToken.token_hash == _hash_token(token))
        )
        principal = row.scalar_one_or_none()
    if principal is None:
        raise ApiError(401, "invalid_token", "Session token is invalid or expired")
    services.presence.touch(principal.id)
    return principal


async def require_principal(
    principal: Annotated[Principal | None, Depends(optional_principal)],
) -> Principal:
    if principal is None:
        raise ApiError(401, "authentication_required", "Send a bearer token from POST /v1/sessions")
    return principal


async def require_admin(
    request: Request, services: Annotated[Services, Depends(get_services)]
) -> None:
    expected = services.settings.admin_token
    token = _extract_token(request)
    if not expected or token is None or not hmac.compare_digest(token, expected):
        raise ApiError(403, "admin_required", "Admin token required")


CurrentPrincipal = Annotated[Principal, Depends(require_principal)]
OptionalPrincipal = Annotated[Principal | None, Depends(optional_principal)]
