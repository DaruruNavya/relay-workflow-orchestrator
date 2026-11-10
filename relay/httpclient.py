"""Tiny stdlib HTTP client. Every call carries a timeout; transport problems raise TransportError."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class TransportError(Exception):
    """Connection refused, DNS failure, timeout, reset... (always treated as transient)."""


@dataclass
class HttpResponse:
    status: int
    body: Any


def request(method: str, url: str, body=None, headers: dict | None = None, timeout: float = 10.0) -> HttpResponse:
    if not url.lower().startswith(("http://", "https://")):
        raise TransportError(f"unsupported URL scheme: {url!r}")
    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
    hdrs.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return HttpResponse(resp.status, _parse(resp.read()))
    except urllib.error.HTTPError as e:
        return HttpResponse(e.code, _parse(e.read()))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise TransportError(f"{type(e).__name__}: {e}") from e


def _parse(raw: bytes):
    text = raw.decode(errors="replace")
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
