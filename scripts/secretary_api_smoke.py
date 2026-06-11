#!/usr/bin/env python3
"""Smoke-test Secretary's local Hermes API-server integration path.

This intentionally uses only the Python standard library so it can run from a
fresh editable Hermes checkout without adding another client dependency.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


BASE_URL = _env("API_SERVER_URL", "http://127.0.0.1:8642").rstrip("/")
API_KEY = _env("API_SERVER_KEY")
SESSION_KEY = _env("HERMES_SESSION_KEY", "secretary:user:dev-jc")
PROMPT = _env("HERMES_API_SMOKE_PROMPT", "Reply with one short sentence.")
TITLE_PREFIX = _env("HERMES_API_SMOKE_TITLE_PREFIX", "Secretary local smoke")


def _request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    if not API_KEY:
        raise SystemExit("API_SERVER_KEY is required")

    data = None
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "X-Hermes-Session-Key": SESSION_KEY,
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {path} failed with HTTP {exc.code}: {raw}") from exc


def main() -> int:
    capabilities = _request("GET", "/v1/capabilities")
    session_id = f"secretary_smoke_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    title = f"{TITLE_PREFIX} {session_id}"
    created = _request(
        "POST",
        "/api/sessions",
        {"id": session_id, "title": title},
    )
    session = created.get("session") or {}
    if session.get("id") != session_id:
        raise SystemExit(f"unexpected create response: {created}")

    chat = _request("POST", f"/api/sessions/{session_id}/chat", {"message": PROMPT})
    messages = _request("GET", f"/api/sessions/{session_id}/messages")
    payload = {
        "ok": True,
        "baseUrl": BASE_URL,
        "sessionKey": SESSION_KEY,
        "sessionId": session_id,
        "title": title,
        "capabilities": {
            "sessionCreate": bool(
                (capabilities.get("endpoints") or {}).get("session_create")
            ),
            "sessionChat": bool((capabilities.get("endpoints") or {}).get("session_chat")),
        },
        "assistantPreview": ((chat.get("message") or {}).get("content") or "")[:240],
        "messageCount": len(messages.get("data") or []),
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
