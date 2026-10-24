"""Auth and input policy for the TripoSR worker.

Kept free of torch/fastapi/PIL so it can be unit-tested anywhere (tests/test_triposr_security.py).

* The worker refuses to start without a bearer token (`require_token`), so auth can never be
  silently off -- compose's `${TSR_API_TOKEN:?...}` is a second guard, not the only one.
* The token is compared in constant time (`bearer_ok`, hmac.compare_digest).
* The worker never fetches a URL on a client's behalf (SSRF into the LAN/tailnet): a request that
  carries a URL input is refused with 422 (`url_input_keys`, `URL_INPUT_REFUSAL`).
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from typing import Any

TOKEN_ENV = "TSR_API_TOKEN"
URL_INPUT_KEYS = ("image_url", "url")
URL_INPUT_REFUSAL = (
    "URL inputs are not accepted: this worker never fetches URLs. "
    "Send the image bytes as multipart `image=@file` or JSON `image_b64`."
)
NO_TOKEN_REFUSAL = (
    "triposr-worker: TSR_API_TOKEN is empty or unset; refusing to start without a bearer token. "
    "Set it in .env next to docker-compose.yml (see .env.example)."
)


def require_token(environ: Mapping[str, str] | None = None) -> str:
    """Return the configured bearer token, or exit the process if there is none."""
    env = os.environ if environ is None else environ
    token = (env.get(TOKEN_ENV) or "").strip()
    if not token:
        raise SystemExit(NO_TOKEN_REFUSAL)
    return token


def bearer_ok(authorization: str | None, token: str) -> bool:
    """Constant-time check of an `Authorization` header against `Bearer <token>`."""
    if not token:
        return False
    # bytes, so a non-ASCII header compares as unequal instead of raising TypeError
    return hmac.compare_digest((authorization or "").encode("utf-8", "surrogateescape"), f"Bearer {token}".encode())


def url_input_keys(params: Mapping[str, Any]) -> list[str]:
    """The URL-input fields a request carries (any value, even empty, is refused)."""
    return [k for k in URL_INPUT_KEYS if k in params]
