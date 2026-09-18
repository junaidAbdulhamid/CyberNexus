"""Authentication stub.

Two mechanisms, both intentionally simple, because this is a demo boundary and
pretending otherwise would be worse than saying so:

* **API key** in ``X-API-Key`` (or ``?api_key=`` for WebSockets, which cannot
  carry custom headers from a browser);
* **JWT** issued by ``POST /api/auth/token`` against that same key, so the
  frontend can hold a short-lived bearer token rather than the long-lived key.

What this is *not*: identity.  There are no users, no roles, no revocation and
no audit trail.  ``README.md`` sets out what production needs — OIDC against the
organisation's IdP, RBAC scoped per site/subnet, and per-action audit — and none
of it is implemented here.  The one real protection this does provide is that
the demo is not open by default.
"""
from __future__ import annotations

import time
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from cnmap.config import settings

bearer_scheme = HTTPBearer(auto_error=False)

#: Coarse roles, in the payload but not yet enforced anywhere: the shape a real
#: RBAC implementation would slot into.
ROLES = ("viewer", "analyst", "admin")


def issue_token(subject: str = "demo", role: str = "analyst",
                ttl_s: Optional[int] = None) -> dict:
    now = int(time.time())
    ttl = ttl_s or settings.jwt_ttl_s
    payload = {
        "sub": subject, "role": role if role in ROLES else "viewer",
        "iat": now, "exp": now + ttl, "iss": "cybernexus-map",
    }
    token = jwt.encode(payload, settings.jwt_secret, algorithm="HS256")
    return {"access_token": token, "token_type": "bearer", "expires_in": ttl,
            "role": payload["role"]}


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"],
                          options={"require": ["exp", "sub"]})
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "token expired")
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"invalid token: {exc}")


def _principal_from_key(key: str) -> dict:
    return {"sub": "api-key", "role": "analyst", "auth": "api_key"}


def authenticate(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    api_key: Optional[str] = Query(None, description="API key (WebSocket / demo use)"),
) -> dict:
    """FastAPI dependency: returns the principal or raises 401."""
    if not settings.auth_enabled:
        return {"sub": "anonymous", "role": "admin", "auth": "disabled"}

    header_key = request.headers.get("x-api-key")
    key = header_key or api_key
    if key and key == settings.api_key:
        return _principal_from_key(key)

    if credentials and credentials.scheme.lower() == "bearer":
        payload = decode_token(credentials.credentials)
        return {"sub": payload.get("sub", "?"), "role": payload.get("role", "viewer"),
                "auth": "jwt"}

    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        "missing credentials: send X-API-Key, ?api_key=, or a bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


def authenticate_ws(token: Optional[str], api_key: Optional[str]) -> Optional[dict]:
    """WebSocket variant: returns the principal, or None to reject."""
    if not settings.auth_enabled:
        return {"sub": "anonymous", "role": "admin", "auth": "disabled"}
    if api_key and api_key == settings.api_key:
        return _principal_from_key(api_key)
    if token:
        try:
            payload = decode_token(token)
        except HTTPException:
            return None
        return {"sub": payload.get("sub", "?"), "role": payload.get("role", "viewer"),
                "auth": "jwt"}
    return None


def require_role(*allowed: str):
    """Dependency factory for role-gated routes.

    Used on the mutating endpoints.  With a single shared API key mapping to
    ``analyst`` this is close to decorative today — it exists so that the call
    sites are already correct when real identities arrive.
    """
    def dependency(principal: dict = Depends(authenticate)) -> dict:
        if principal.get("role") not in allowed and principal.get("role") != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"role '{principal.get('role')}' cannot perform this action")
        return principal

    return dependency
