"""Secretary Google Workspace auth and approval support.

Secretary private Hermes runtimes are trusted backend peers for one owning user.
This module lets those runtimes read that user's Google Workspace OAuth fields
directly from Secretary Postgres, refresh tokens, and feed Google remote MCP
servers with bearer tokens.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"

GOOGLE_WORKSPACE_REQUIRED_SCOPES = {
    "gmail": (
        "https://www.googleapis.com/auth/gmail.modify",
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.compose",
    ),
    "calendar": (
        "https://www.googleapis.com/auth/calendar",
        "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
        "https://www.googleapis.com/auth/calendar.events.freebusy",
        "https://www.googleapis.com/auth/calendar.events.readonly",
    ),
    "people": (
        "https://www.googleapis.com/auth/contacts",
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/directory.readonly",
        "https://www.googleapis.com/auth/userinfo.profile",
    ),
    "contacts": (
        "https://www.googleapis.com/auth/contacts",
        "https://www.googleapis.com/auth/contacts.readonly",
        "https://www.googleapis.com/auth/directory.readonly",
        "https://www.googleapis.com/auth/userinfo.profile",
    ),
}

GOOGLE_WORKSPACE_CONNECT_SCOPES = {
    "gmail": "https://www.googleapis.com/auth/gmail.modify",
    "calendar": "https://www.googleapis.com/auth/calendar",
    "people": "https://www.googleapis.com/auth/contacts",
    "contacts": "https://www.googleapis.com/auth/contacts",
}

GOOGLE_MCP_MUTATING_TOOLS = {
    "gmail": {
        "create_draft",
        "create_label",
        "label_message",
        "label_thread",
        "unlabel_message",
        "unlabel_thread",
    },
    "calendar": {
        "create_event",
        "delete_event",
        "respond_to_event",
        "update_event",
    },
    "people": set(),
}

_AUTH_STORE: "SecretaryGoogleWorkspaceAuthStore | None" = None
_AUTH_STORE_LOCK = threading.Lock()


class SecretaryGoogleWorkspaceAuthError(RuntimeError):
    """Raised when the private runtime cannot obtain a Google access token."""


@dataclass(frozen=True)
class GoogleWorkspaceToken:
    access_token: str
    expires_at: datetime | None
    account_email: str | None
    granted_scopes: tuple[str, ...]


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SecretaryGoogleWorkspaceAuthError(f"{name} is required.")
    return value


def _normalize_service(service: str | None) -> str:
    normalized = (service or "").strip().lower()
    if normalized == "contacts":
        return "people"
    if normalized not in {"gmail", "calendar", "people"}:
        raise SecretaryGoogleWorkspaceAuthError(
            f"Unsupported Google Workspace MCP service: {service!r}"
        )
    return normalized


def _b64url_decode(value: str) -> bytes:
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _encryption_key() -> bytes:
    secret = _required_env("GOOGLE_OAUTH_TOKEN_ENCRYPTION_KEY")
    return hashlib.sha256(secret.encode("utf-8")).digest()


def decrypt_workspace_token(payload: str) -> str:
    parts = payload.split(".")
    if len(parts) != 3:
        raise SecretaryGoogleWorkspaceAuthError("Invalid encrypted token payload.")
    iv, auth_tag, encrypted = (_b64url_decode(part) for part in parts)
    return AESGCM(_encryption_key()).decrypt(iv, encrypted + auth_tag, None).decode(
        "utf-8"
    )


def encrypt_workspace_token(value: str) -> str:
    iv = os.urandom(12)
    encrypted = AESGCM(_encryption_key()).encrypt(iv, value.encode("utf-8"), None)
    ciphertext, auth_tag = encrypted[:-16], encrypted[-16:]
    return ".".join(
        [_b64url_encode(iv), _b64url_encode(auth_tag), _b64url_encode(ciphertext)]
    )


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


class SecretaryGoogleWorkspaceAuthStore:
    """Direct Postgres-backed Google Workspace token provider."""

    def __init__(self) -> None:
        self.runtime_id = _required_env("SECRETARY_RUNTIME_ID")
        self.database_url = _required_env("SECRETARY_RUNTIME_DATABASE_URL")
        self.google_client_id = _required_env("GOOGLE_CLIENT_ID")
        self.google_client_secret = _required_env("GOOGLE_CLIENT_SECRET")
        self._cache: dict[str, GoogleWorkspaceToken] = {}
        self._cache_lock = threading.Lock()

    async def get_access_token(
        self, service: str, *, force_refresh: bool = False
    ) -> GoogleWorkspaceToken:
        normalized = _normalize_service(service)
        cached = self._get_cached(normalized)
        if cached is not None and not force_refresh:
            return cached
        token = await asyncio.to_thread(self._load_or_refresh_token, normalized)
        with self._cache_lock:
            self._cache[normalized] = token
        return token

    def invalidate(self, service: str | None = None) -> None:
        with self._cache_lock:
            if service:
                self._cache.pop(_normalize_service(service), None)
            else:
                self._cache.clear()

    def _get_cached(self, service: str) -> GoogleWorkspaceToken | None:
        with self._cache_lock:
            token = self._cache.get(service)
        if not token:
            return None
        if token.expires_at is None:
            return None
        if token.expires_at <= datetime.now(timezone.utc) + timedelta(seconds=60):
            return None
        return token

    def _connect(self):
        try:
            import psycopg
            from psycopg.rows import dict_row
        except Exception as exc:  # pragma: no cover - dependency error path
            raise SecretaryGoogleWorkspaceAuthError(
                "psycopg is required for Secretary Google Workspace auth."
            ) from exc
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def _load_connection_row(self) -> dict[str, Any]:
        query = """
            select gwc.*
            from google_workspace_connections gwc
            join secretary_bot_runtimes rt
              on rt.owner_user_id = gwc.user_id
            where rt.id = %s
              and rt.deleted_at is null
              and gwc.revoked_at is null
            limit 1
        """
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (self.runtime_id,))
                row = cur.fetchone()
        if not row:
            raise SecretaryGoogleWorkspaceAuthError(
                "Google Workspace is not connected for this private Hermes runtime."
            )
        return dict(row)

    def _load_or_refresh_token(self, service: str) -> GoogleWorkspaceToken:
        row = self._load_connection_row()
        self._assert_scope(row, service)
        expires_at = _parse_timestamp(row.get("token_expires_at"))
        access_encrypted = row.get("access_token_encrypted")
        refresh_encrypted = row.get("refresh_token_encrypted")
        if not refresh_encrypted:
            raise SecretaryGoogleWorkspaceAuthError(
                "Google Workspace reconnect is required: refresh token is missing."
            )
        if (
            access_encrypted
            and expires_at
            and expires_at > datetime.now(timezone.utc) + timedelta(seconds=60)
        ):
            return GoogleWorkspaceToken(
                access_token=decrypt_workspace_token(access_encrypted),
                expires_at=expires_at,
                account_email=row.get("email"),
                granted_scopes=tuple(row.get("granted_scopes") or ()),
            )
        return self._refresh_and_persist(row, service)

    def _assert_scope(self, row: dict[str, Any], service: str) -> None:
        granted = set(row.get("granted_scopes") or ())
        required = GOOGLE_WORKSPACE_CONNECT_SCOPES[service]
        if required not in granted:
            raise SecretaryGoogleWorkspaceAuthError(
                f"Google Workspace reconnect is required: missing scope {required}."
            )

    def assert_required_mcp_scopes(self, service: str) -> None:
        normalized = _normalize_service(service)
        row = self._load_connection_row()
        granted = set(row.get("granted_scopes") or ())
        missing = [
            scope
            for scope in GOOGLE_WORKSPACE_REQUIRED_SCOPES[normalized]
            if scope not in granted
        ]
        if missing:
            raise SecretaryGoogleWorkspaceAuthError(
                "Google Workspace reconnect is required: missing scope "
                f"{missing[0]}."
            )

    def _refresh_and_persist(
        self, row: dict[str, Any], service: str
    ) -> GoogleWorkspaceToken:
        refresh_token = decrypt_workspace_token(row["refresh_token_encrypted"])
        response = httpx.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "client_id": self.google_client_id,
                "client_secret": self.google_client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=30.0,
        )
        if response.status_code >= 400:
            raise SecretaryGoogleWorkspaceAuthError(
                "Google Workspace reconnect is required: token refresh failed."
            )
        payload = response.json()
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise SecretaryGoogleWorkspaceAuthError(
                "Google Workspace reconnect is required: refresh returned no access token."
            )
        expires_in = payload.get("expires_in")
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=int(expires_in) if isinstance(expires_in, (int, float)) else 3600
        )
        rotated_refresh = payload.get("refresh_token")
        refresh_encrypted = (
            encrypt_workspace_token(rotated_refresh)
            if isinstance(rotated_refresh, str) and rotated_refresh
            else row["refresh_token_encrypted"]
        )
        access_encrypted = encrypt_workspace_token(access_token)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    update google_workspace_connections
                    set access_token_encrypted = %s,
                        refresh_token_encrypted = %s,
                        token_expires_at = %s,
                        updated_at = now()
                    where id = %s
                    """,
                    (access_encrypted, refresh_encrypted, expires_at, row["id"]),
                )
            conn.commit()
        return GoogleWorkspaceToken(
            access_token=access_token,
            expires_at=expires_at,
            account_email=row.get("email"),
            granted_scopes=tuple(row.get("granted_scopes") or ()),
        )


def get_auth_store() -> SecretaryGoogleWorkspaceAuthStore:
    global _AUTH_STORE
    with _AUTH_STORE_LOCK:
        if _AUTH_STORE is None:
            _AUTH_STORE = SecretaryGoogleWorkspaceAuthStore()
        return _AUTH_STORE


class SecretaryGoogleWorkspacePostgresAuth(httpx.Auth):
    """httpx auth object for Google remote MCP servers."""

    def __init__(self, service: str):
        self.service = _normalize_service(service)

    async def async_auth_flow(self, request: httpx.Request):
        token = await get_auth_store().get_access_token(self.service)
        request.headers["Authorization"] = f"Bearer {token.access_token}"
        response = yield request
        if response.status_code not in {401, 403}:
            return
        get_auth_store().invalidate(self.service)
        token = await get_auth_store().get_access_token(
            self.service, force_refresh=True
        )
        request.headers["Authorization"] = f"Bearer {token.access_token}"
        yield request


def build_mcp_http_auth(auth_config: Any) -> httpx.Auth:
    if not isinstance(auth_config, dict):
        raise SecretaryGoogleWorkspaceAuthError(
            "secretary_google_workspace_postgres auth must be an object."
        )
    return SecretaryGoogleWorkspacePostgresAuth(str(auth_config.get("service") or ""))


def auth_type_from_config(auth_config: Any) -> str:
    if isinstance(auth_config, str):
        return auth_config.lower().strip()
    if isinstance(auth_config, dict):
        return str(auth_config.get("type") or "").lower().strip()
    return ""


def is_secretary_google_workspace_auth(auth_config: Any) -> bool:
    return auth_type_from_config(auth_config) == "secretary_google_workspace_postgres"


def service_from_auth_config(auth_config: Any) -> str | None:
    if not is_secretary_google_workspace_auth(auth_config):
        return None
    if isinstance(auth_config, dict):
        return _normalize_service(str(auth_config.get("service") or ""))
    return None


def validate_google_mcp_required_scopes(auth_config: Any) -> str | None:
    service = service_from_auth_config(auth_config)
    if not service:
        return None
    try:
        get_auth_store().assert_required_mcp_scopes(service)
    except Exception as exc:
        return format_auth_error(exc)
    return None


def google_mcp_tool_requires_approval(auth_config: Any, tool_name: str) -> bool:
    service = service_from_auth_config(auth_config)
    if not service:
        return False
    return tool_name in GOOGLE_MCP_MUTATING_TOOLS.get(service, set())


def build_google_mcp_approval_data(
    *, server_name: str, tool_name: str, args: dict[str, Any], auth_config: Any
) -> dict[str, Any]:
    service = service_from_auth_config(auth_config) or "google"
    approval_id = f"gws_{uuid.uuid4().hex}"
    action = _human_google_action(service, tool_name)
    title = "Approval needed"
    message = f"Secretary wants to {action}."
    envelope = {
        "version": 1,
        "component": "approval_card",
        "interactionState": "awaiting_approval",
        "title": title,
        "message": message,
        "props": {
            "provider": "google_workspace",
            "service": service,
            "serverName": server_name,
            "toolName": tool_name,
            "arguments": args,
            "display": {
                "sections": [
                    {
                        "title": "Change",
                        "items": [message],
                    }
                ]
            },
        },
        "actions": [
            {
                "id": "approve",
                "label": "Approve",
                "kind": "approve",
                "variant": "default",
                "risk": "write",
                "payload": {"approvalId": approval_id},
            },
            {
                "id": "reject",
                "label": "Reject",
                "kind": "reject",
                "variant": "secondary",
                "risk": "write",
                "payload": {"approvalId": approval_id},
            },
        ],
        "density": "compact",
    }
    return {
        "approval_id": approval_id,
        "command": f"mcp:{server_name}/{tool_name}",
        "description": message,
        "pattern_key": f"google_workspace_mcp:{server_name}/{tool_name}",
        "pattern_keys": [f"google_workspace_mcp:{server_name}/{tool_name}"],
        "server_name": server_name,
        "tool_name": tool_name,
        "arguments": args,
        "ui": envelope,
    }


def _human_google_action(service: str, tool_name: str) -> str:
    normalized_service = service.strip().lower()
    normalized_tool = tool_name.strip().lower()
    if normalized_service == "calendar":
        return {
            "create_event": "add a calendar event",
            "update_event": "update a calendar event",
            "delete_event": "delete a calendar event",
            "respond_to_event": "respond to a calendar invitation",
        }.get(normalized_tool, "change your calendar")
    if normalized_service == "gmail":
        return {
            "create_draft": "create an email draft",
            "create_label": "create an email label",
            "label_message": "update email labels",
            "label_thread": "update email labels",
            "unlabel_message": "update email labels",
            "unlabel_thread": "update email labels",
        }.get(normalized_tool, "change your email")
    return "make this change"


def require_google_mcp_approval(
    *, server_name: str, tool_name: str, args: dict[str, Any], auth_config: Any
) -> str | None:
    """Block until the user approves a mutating Google MCP call.

    Returns an error string when execution must not continue.
    """

    if not google_mcp_tool_requires_approval(auth_config, tool_name):
        return None
    from tools.approval import (
        _await_gateway_decision,
        _gateway_notify_cbs,
        _lock as approval_lock,
        get_current_session_key,
    )

    session_key = get_current_session_key(default="")
    if not session_key:
        return "I need an approval screen before I can make that change."
    with approval_lock:
        notify_cb = _gateway_notify_cbs.get(session_key)
    if notify_cb is None:
        return (
            "I need an approval screen before I can make that change, but it "
            "is not available right now."
        )
    approval_data = build_google_mcp_approval_data(
        server_name=server_name,
        tool_name=tool_name,
        args=args if isinstance(args, dict) else {},
        auth_config=auth_config,
    )
    decision = _await_gateway_decision(
        session_key, notify_cb, approval_data, surface="secretary_google_workspace"
    )
    choice = decision.get("choice")
    if decision.get("notify_failed"):
        return "I could not show the approval screen. Please try again."
    if not decision.get("resolved") or choice in {None, "deny", "reject"}:
        return (
            "That change was not approved. I will not make it unless you ask "
            "again."
        )
    return None


def approval_event_from_data(approval_data: Dict[str, Any]) -> Dict[str, Any]:
    envelope = approval_data.get("ui")
    if not isinstance(envelope, dict):
        envelope = build_google_mcp_approval_data(
            server_name=str(approval_data.get("server_name") or "google"),
            tool_name=str(approval_data.get("tool_name") or "tool"),
            args=approval_data.get("arguments")
            if isinstance(approval_data.get("arguments"), dict)
            else {},
            auth_config={"type": "secretary_google_workspace_postgres", "service": "gmail"},
        )["ui"]
    return {
        "approval_id": approval_data.get("approval_id"),
        "ui": envelope,
        "preview": envelope.get("message") if isinstance(envelope, dict) else "",
    }


def format_auth_error(exc: BaseException) -> str:
    raw = str(exc).strip()
    lowered = raw.lower()
    if "missing scope" in lowered or "scope" in lowered:
        message = "Please reconnect Google in Secretary so I can get the access needed for that."
    elif "refresh token" in lowered or "token refresh" in lowered:
        message = "Please reconnect Google in Secretary before I try that again."
    elif "not connected" in lowered or "reconnect" in lowered or "revoked" in lowered:
        message = "Please reconnect Google in Secretary before I try that again."
    else:
        message = raw or "Please check your Google connection in Secretary."
    return json.dumps({"error": message}, ensure_ascii=False)
