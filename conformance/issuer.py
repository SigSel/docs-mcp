"""An OAuth issuer for the tests, shaped like Entra ID: OpenID discovery and a key set over HTTP, and access tokens
signed with its keys (SPEC.md §2.2)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

AUDIENCE = "6a1e2b3c-0000-4000-8000-000000000001"  # an Entra ID client ID
SCOPE = "api://docs-mcp/docs.read"


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Issuer:
    def __init__(self) -> None:
        self.keys: dict[str, tuple[str, Any]] = {}  # kid -> (alg, private key)
        self.published: list[str] = []
        self.fetches = 0  # of the key set
        issuer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/tenant/v2.0/.well-known/openid-configuration":
                    body = {"issuer": issuer.url, "jwks_uri": f"{issuer.base}/tenant/discovery/v2.0/keys"}
                elif self.path == "/tenant/discovery/v2.0/keys":
                    issuer.fetches += 1
                    body = {"keys": [issuer.jwk(kid) for kid in issuer.published]}
                else:
                    self.send_error(404)
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.url = f"{self.base}/tenant/v2.0"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.add_key("k1")

    def stop(self) -> None:
        self.server.shutdown()

    def add_key(self, kid: str, alg: str = "RS256", publish: bool = True) -> None:
        if alg.startswith("ES"):
            key: Any = ec.generate_private_key(ec.SECP256R1() if alg == "ES256" else ec.SECP384R1())
        else:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.keys[kid] = (alg, key)
        if publish:
            self.published.append(kid)

    def jwk(self, kid: str) -> dict[str, Any]:
        alg, key = self.keys[kid]
        algorithm = ECAlgorithm if alg.startswith("ES") else RSAAlgorithm
        return {**algorithm.to_jwk(key.public_key(), as_dict=True), "kid": kid, "use": "sig", "alg": alg}

    def claims(self, **changes: Any) -> dict[str, Any]:
        now = int(time.time())
        claims = {"iss": self.url, "aud": AUDIENCE, "exp": now + 300, "nbf": now - 10, "iat": now - 10,
                  "sub": "user-1", "scp": "docs.read"}
        claims.update(changes)
        return {k: v for k, v in claims.items() if v is not None}

    def token(self, kid: str = "k1", sign_with: str | None = None, **changes: Any) -> str:
        """A token from key `kid` (or signed with another key's private half), with claims changed or, for None,
        removed."""
        alg, key = self.keys[sign_with or kid]
        return jwt.encode(self.claims(**changes), key, algorithm=alg, headers={"kid": kid})

    def unsigned(self, **changes: Any) -> str:
        header = b64(json.dumps({"alg": "none", "kid": "k1"}).encode())
        return f"{header}.{b64(json.dumps(self.claims(**changes)).encode())}."

    def hmac_with_public_key(self) -> str:
        """The classic algorithm confusion: HS256, keyed with the issuer's public key."""
        _, key = self.keys["k1"]
        secret = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        signing_input = f"{b64(json.dumps({'alg': 'HS256', 'kid': 'k1'}).encode())}.{b64(json.dumps(self.claims()).encode())}"
        return f"{signing_input}.{b64(hmac.new(secret, signing_input.encode(), hashlib.sha256).digest())}"
