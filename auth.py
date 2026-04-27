#!/usr/bin/env python3
"""
Knight Shift — Clerk Auth Integration
=====================================
Verifies Clerk session JWTs on every protected request and lazily syncs
the authenticated Clerk user into our local `users` table.

Strategy: networkless verification. Clerk publishes a JWKS at the
frontend API URL; we verify session JWTs locally with PyJWT. The Clerk
backend SDK is used only for one-shot user lookups when we don't have
the email cached.

Decorator usage:
    from auth import require_auth, current_user_id

    @app.route("/protected")
    @require_auth
    def view():
        uid = current_user_id()
        ...
"""

import logging
from functools import wraps
from typing import Optional
from urllib.parse import quote

import jwt
from jwt import PyJWKClient, InvalidTokenError
from flask import request, redirect, g, jsonify, abort

from config import (
    CLERK_SECRET_KEY, CLERK_PUBLISHABLE_KEY,
    CLERK_SIGN_IN_URL, CLERK_AUTHORIZED_PARTIES,
)
from db import session_scope
from models import User

logger = logging.getLogger("nightshift.auth")


# ---------------------------------------------------------------------------
# JWKS client (cached) — derives Clerk's frontend API URL from publishable key
# ---------------------------------------------------------------------------

def clerk_frontend_api_host() -> str:
    """Reverse-engineer Clerk's frontend API host from the publishable key.

    A Clerk publishable key has the form:
        pk_(test|live)_<base64url-encoded-domain>$
    The trailing $ is padding. Decoding the middle portion gives the
    frontend API host (e.g. 'verb-noun-12.clerk.accounts.dev' for dev).
    Returned without scheme — caller adds https:// as needed.
    """
    if not CLERK_PUBLISHABLE_KEY:
        raise RuntimeError("CLERK_PUBLISHABLE_KEY is not set")
    parts = CLERK_PUBLISHABLE_KEY.split("_", 2)
    if len(parts) != 3 or parts[0] != "pk":
        raise RuntimeError("Invalid CLERK_PUBLISHABLE_KEY format")
    encoded = parts[2].rstrip("$")
    import base64
    padding = "=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding).decode("ascii").rstrip("$")


def _frontend_api_url() -> str:
    return f"https://{clerk_frontend_api_host()}"


_jwks_client: Optional[PyJWKClient] = None


def _get_jwks_client() -> PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = PyJWKClient(f"{_frontend_api_url()}/.well-known/jwks.json")
    return _jwks_client


# ---------------------------------------------------------------------------
# Session JWT verification
# ---------------------------------------------------------------------------

class AuthError(Exception):
    """Raised when the request can't be authenticated."""


def _read_session_token() -> Optional[str]:
    """Pull Clerk's session JWT from cookie or Authorization header."""
    token = request.cookies.get("__session")
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return None


def verify_session(token: str) -> dict:
    """Verify a Clerk session JWT and return its decoded claims.

    Raises AuthError on any verification failure.
    """
    if not token:
        raise AuthError("missing session token")
    try:
        signing_key = _get_jwks_client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            # Clerk sets 'azp' (authorized party) instead of standard 'aud';
            # PyJWT's audience check would reject. We validate azp manually.
            options={"verify_aud": False},
        )
    except InvalidTokenError as exc:
        raise AuthError(f"invalid session token: {exc}") from exc

    # azp must be one of our authorized origins (CSRF defense per Clerk docs).
    if CLERK_AUTHORIZED_PARTIES:
        azp = claims.get("azp")
        if azp and azp not in CLERK_AUTHORIZED_PARTIES:
            raise AuthError(f"unauthorized azp: {azp}")

    return claims


# ---------------------------------------------------------------------------
# User row sync
# ---------------------------------------------------------------------------

_clerk_sdk = None


def _clerk():
    """Lazy-init the Clerk backend SDK for user lookups."""
    global _clerk_sdk
    if _clerk_sdk is None:
        if not CLERK_SECRET_KEY:
            raise RuntimeError("CLERK_SECRET_KEY is not set")
        from clerk_backend_api import Clerk
        _clerk_sdk = Clerk(bearer_auth=CLERK_SECRET_KEY)
    return _clerk_sdk


def _fetch_clerk_user_email_and_name(clerk_user_id: str) -> tuple[str, Optional[str]]:
    """One-shot Clerk API call to retrieve a user's primary email + name."""
    user = _clerk().users.get(user_id=clerk_user_id)
    primary_id = getattr(user, "primary_email_address_id", None)
    email = None
    for ea in (user.email_addresses or []):
        if primary_id and ea.id == primary_id:
            email = ea.email_address
            break
    if email is None and user.email_addresses:
        email = user.email_addresses[0].email_address
    if not email:
        raise AuthError(f"Clerk user {clerk_user_id} has no email address")
    name = " ".join(filter(None, [user.first_name, user.last_name])) or None
    return email, name


def _sync_user(clerk_user_id: str) -> int:
    """Return our local users.id for this Clerk user, creating/linking as needed."""
    with session_scope() as session:
        user = session.query(User).filter(User.clerk_user_id == clerk_user_id).one_or_none()
        if user is not None:
            return user.id

        # Not yet linked — fetch email from Clerk and try to match an existing
        # row (someone may have submitted via email before signing in).
        email, name = _fetch_clerk_user_email_and_name(clerk_user_id)
        user = session.query(User).filter(User.email == email.lower()).one_or_none()
        if user is None:
            user = User(email=email.lower(), name=name, clerk_user_id=clerk_user_id)
            session.add(user)
        else:
            user.clerk_user_id = clerk_user_id
            if name and not user.name:
                user.name = name
        session.flush()
        return user.id


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def _redirect_to_sign_in():
    """Send the user to Clerk's hosted sign-in page, with return URL."""
    if not CLERK_SIGN_IN_URL:
        return jsonify({"error": "auth not configured"}), 500
    return_to = quote(request.url, safe="")
    sep = "&" if "?" in CLERK_SIGN_IN_URL else "?"
    return redirect(f"{CLERK_SIGN_IN_URL}{sep}redirect_url={return_to}")


def require_auth(view_func):
    """Verify the request, populate flask.g.user_id, or redirect to sign-in.

    For HTML routes (Accept: text/html), unauthenticated users are 302'd to
    Clerk's hosted sign-in page. For JSON / API requests, returns 401 JSON.
    """
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        token = _read_session_token()
        try:
            claims = verify_session(token)
        except AuthError as exc:
            logger.info("Auth rejected (%s): %s", request.path, exc)
            wants_html = "text/html" in (request.headers.get("Accept") or "")
            if wants_html:
                return _redirect_to_sign_in()
            return jsonify({"error": "unauthorized"}), 401

        clerk_user_id = claims.get("sub")
        if not clerk_user_id:
            abort(401)

        import sys, os
        env_secret = os.environ.get("CLERK_SECRET_KEY", "")
        from config import CLERK_SECRET_KEY as cfg_secret
        print(
            f"!!! REQ DEBUG sub={clerk_user_id!r} "
            f"env_secret_len={len(env_secret)} env_secret_pfx={env_secret[:12]!r} "
            f"cfg_secret_len={len(cfg_secret)} cfg_secret_pfx={cfg_secret[:12]!r} "
            f"pid={os.getpid()}",
            file=sys.stderr, flush=True,
        )
        try:
            g.clerk_user_id = clerk_user_id
            g.clerk_claims = claims
            g.user_id = _sync_user(clerk_user_id)
        except Exception as exc:
            import traceback
            print(f"!!! USER SYNC FAILED for {clerk_user_id!r}: {exc!r}", file=sys.stderr, flush=True)
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()
            logger.error("User sync failed for %s: %s", clerk_user_id, exc, exc_info=True)
            return jsonify({"error": str(exc) or "user provisioning failed"}), 500

        return view_func(*args, **kwargs)
    return wrapper


def current_user_id() -> int:
    """Return the local users.id for the current request (must be inside @require_auth)."""
    uid = getattr(g, "user_id", None)
    if uid is None:
        raise RuntimeError("current_user_id() called outside @require_auth")
    return uid
