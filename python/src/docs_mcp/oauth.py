"""docs-mcp as an OAuth resource server: the protected resource metadata, and the checks on access tokens
(SPEC.md §2.2)."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import ssl
import time
from typing import Any

import httpx2
import jwt
import truststore

from .config import Config, https_or_local, split_url

log = logging.getLogger("docs_mcp")

METADATA = "/.well-known/oauth-protected-resource"
# alg -> (kty, crv) of the key it needs. Never `none` or HMAC.
ALGORITHMS = {
    **{alg: ("RSA", None) for alg in ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512")},
    "ES256": ("EC", "P-256"),
    "ES384": ("EC", "P-384"),
}
LEEWAY = 60
KEYS_MAX_AGE = 3600
UNKNOWN_KID_EVERY = 60  # tokens with made-up kids cannot keep us fetching
RETRY_AFTER_FAILURE = 10
MAX_BYTES = 1_000_000
BASE64URL = re.compile(r"[A-Za-z0-9_-]*")


class Refused(Exception):
    def __init__(self, status: int, error: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.error, self.reason = status, error, reason


class Unreachable(Exception):
    """No key set, and the issuer did not hand one over."""


def _invalid(reason: str) -> Refused:
    return Refused(401, "invalid_token", reason)


def metadata_path(public_url: str) -> str:
    return METADATA + split_url(public_url)[2].rstrip("/")


def metadata_url(public_url: str) -> str:
    scheme, authority, _ = split_url(public_url)
    return f"{scheme}://{authority}{metadata_path(public_url)}"


def metadata(config: Config) -> dict[str, Any]:
    out: dict[str, Any] = {"resource": config.public_url, "authorization_servers": [config.oauth_issuer]}
    if config.oauth_scopes:
        out["scopes_supported"] = config.oauth_scopes
    return out | {"bearer_methods_supported": ["header"], "resource_name": config.name}


def _b64decode(part: str) -> bytes:
    if len(part) % 4 == 1:
        raise ValueError("not base64url")
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def _parse(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    """(header, payload, signing input, signature), or `malformed token`."""
    parts = token.split(".")
    if len(parts) != 3 or not all(BASE64URL.fullmatch(p) for p in parts):
        raise _invalid("malformed token")
    try:
        header, payload = json.loads(_b64decode(parts[0])), json.loads(_b64decode(parts[1]))
        signature = _b64decode(parts[2])
    except (ValueError, RecursionError):  # JSON nested too deep to decode, sent by anyone who can reach the port
        raise _invalid("malformed token") from None
    if not (isinstance(header, dict) and isinstance(payload, dict)):
        raise _invalid("malformed token")
    if not (isinstance(header.get("kid"), str) and isinstance(header.get("alg"), str)):
        raise _invalid("malformed token")
    return header, payload, f"{parts[0]}.{parts[1]}".encode(), signature


def _number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _last(scope: str) -> str:
    return scope.rsplit("/", 1)[-1]


def _scopes(payload: dict[str, Any]) -> set[str]:
    raw = payload["scp"] if "scp" in payload else payload.get("scope")
    items = raw.split() if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return {_last(s) for s in items if isinstance(s, str)}


def _fits(key: dict[str, Any], kid: str, alg: str) -> bool:
    kty, crv = ALGORITHMS[alg]
    return (
        key.get("kid") == kid
        and key.get("kty") == kty
        and (crv is None or key.get("crv") == crv)
        and key.get("alg", alg) == alg
        and key.get("use", "sig") == "sig"
    )


class TokenChecker:
    def __init__(self, config: Config) -> None:
        assert config.oauth_issuer
        self.issuer = config.oauth_issuer
        self.audience = config.oauth_audience
        self.scopes = config.oauth_scopes
        self.keys: list[dict[str, Any]] | None = None
        self.next_fetch = 0.0
        self.unknown_kid_fetched = -float("inf")
        self.lock = asyncio.Lock()
        self.client = httpx2.AsyncClient(verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT), timeout=10)

    async def check(self, token: str) -> None:
        """Returns for a token that passes every check, raises Refused or Unreachable otherwise."""
        header, payload, signing_input, signature = _parse(token)
        alg = header["alg"]
        if alg not in ALGORITHMS:
            raise _invalid("unsupported algorithm")
        key = await self._key(header["kid"], alg)
        try:
            jwk = jwt.PyJWK(key, algorithm=alg)  # type: ignore[arg-type]
            verified = jwk.Algorithm.verify(signing_input, jwk.key, signature)
        except (jwt.PyJWTError, ValueError, TypeError):
            verified = False
        if not verified:
            raise _invalid("bad signature")
        if payload.get("iss") != self.issuer:
            raise _invalid("wrong issuer")
        aud = payload.get("aud")
        audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
        if self.audience not in [a for a in audiences if isinstance(a, str)]:
            raise _invalid("wrong audience")
        now = time.time()
        if not _number(exp := payload.get("exp")) or now >= exp + LEEWAY:
            raise _invalid("expired")
        if "nbf" in payload and (not _number(nbf := payload["nbf"]) or nbf > now + LEEWAY):
            raise _invalid("not yet valid")
        have = _scopes(payload)
        for scope in self.scopes:
            if _last(scope) not in have:
                raise Refused(403, "insufficient_scope", f"missing scope {scope}")

    async def _key(self, kid: str, alg: str) -> dict[str, Any]:
        async with self.lock:
            now = time.monotonic()
            if self.keys is None or now >= self.next_fetch:
                await self._fetch()
            assert self.keys is not None
            key = next((k for k in self.keys if _fits(k, kid, alg)), None)
            if key is None and now - self.unknown_kid_fetched >= UNKNOWN_KID_EVERY:
                self.unknown_kid_fetched = now
                await self._fetch()
                key = next((k for k in self.keys if _fits(k, kid, alg)), None)
        if key is None:
            raise _invalid("unknown signing key")
        return key

    async def _fetch(self) -> None:
        """Fetches the key set through OpenID discovery. A failure keeps the old one, if any."""
        try:
            discovery = await self._get_json(self.issuer.rstrip("/") + "/.well-known/openid-configuration")
            if discovery.get("issuer") != self.issuer:
                raise ValueError(f"the discovery document names the issuer {discovery.get('issuer')!r}")
            jwks_uri = discovery.get("jwks_uri")
            if not isinstance(jwks_uri, str):
                raise ValueError("the discovery document has no jwks_uri")
            keys = (await self._get_json(jwks_uri)).get("keys")
            if not isinstance(keys, list):
                raise ValueError("the key set has no keys")
            self.keys = [k for k in keys if isinstance(k, dict)]
            self.next_fetch = time.monotonic() + KEYS_MAX_AGE
        except (httpx2.HTTPError, ValueError) as e:
            log.error("fetching the OAuth issuer's signing keys failed: %s", e)
            self.next_fetch = time.monotonic() + RETRY_AFTER_FAILURE
            if self.keys is None:
                raise Unreachable from e

    async def _get_json(self, url: str) -> dict[str, Any]:
        if not https_or_local(url):
            raise ValueError(f"{url} is not https://")
        body = b""
        async with self.client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError(f"{url} answered {response.status_code}")
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_BYTES:
                    raise ValueError(f"{url} sent more than {MAX_BYTES} bytes")
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError(f"{url} did not send a JSON object")
        return data
