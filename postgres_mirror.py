"""Optional PostgreSQL mirror for Hermes ``state.db``.

SQLite remains the canonical SessionDB store.  This module mirrors committed
session/message rows into Postgres for Secretary-style inspection, reporting,
and future SaaS runtime work without replacing upstream Hermes persistence.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DEFAULT_SCHEMA = "hermes"
DEFAULT_DSN = "postgresql://hermes:hermes@127.0.0.1:55432/hermes"
_SCHEMA_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


SESSION_COLUMNS = (
    "id",
    "source",
    "user_id",
    "model",
    "model_config",
    "system_prompt",
    "parent_session_id",
    "started_at",
    "ended_at",
    "end_reason",
    "message_count",
    "tool_call_count",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "cwd",
    "billing_provider",
    "billing_base_url",
    "billing_mode",
    "estimated_cost_usd",
    "actual_cost_usd",
    "cost_status",
    "cost_source",
    "pricing_version",
    "title",
    "api_call_count",
    "handoff_state",
    "handoff_platform",
    "handoff_error",
    "rewind_count",
    "archived",
)

MESSAGE_COLUMNS = (
    "id",
    "session_id",
    "role",
    "content",
    "tool_call_id",
    "tool_calls",
    "tool_name",
    "timestamp",
    "token_count",
    "finish_reason",
    "reasoning",
    "reasoning_content",
    "reasoning_details",
    "codex_reasoning_items",
    "codex_message_items",
    "platform_message_id",
    "observed",
    "active",
)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _row_to_dict(row: sqlite3.Row | Mapping[str, Any] | Sequence[Any], columns: Sequence[str]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    if isinstance(row, Mapping):
        return dict(row)
    return {columns[index]: row[index] for index in range(min(len(columns), len(row)))}


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


@dataclass(frozen=True)
class PostgresMirrorConfig:
    enabled: bool
    dsn: str
    schema: str
    home_key: str
    strict: bool
    connect_timeout_seconds: int
    statement_timeout_ms: int

    @classmethod
    def from_env(cls, *, db_path: Path | None = None) -> "PostgresMirrorConfig":
        schema = os.getenv("HERMES_POSTGRES_SCHEMA", DEFAULT_SCHEMA).strip() or DEFAULT_SCHEMA
        if not _SCHEMA_RE.match(schema):
            raise ValueError(
                "HERMES_POSTGRES_SCHEMA must be a simple PostgreSQL identifier "
                "(letters, numbers, and underscores; cannot start with a number)."
            )
        home = db_path.parent if db_path else get_hermes_home()
        default_home_key = home.name or "default"
        return cls(
            enabled=_truthy(os.getenv("HERMES_POSTGRES_MIRROR_ENABLED")),
            dsn=os.getenv("HERMES_POSTGRES_DSN", DEFAULT_DSN).strip() or DEFAULT_DSN,
            schema=schema,
            home_key=os.getenv("HERMES_POSTGRES_HOME_KEY", default_home_key).strip()
            or default_home_key,
            strict=_truthy(os.getenv("HERMES_POSTGRES_STRICT")),
            connect_timeout_seconds=int(os.getenv("HERMES_POSTGRES_CONNECT_TIMEOUT", "3")),
            statement_timeout_ms=int(os.getenv("HERMES_POSTGRES_STATEMENT_TIMEOUT_MS", "5000")),
        )


class PostgresMirror:
    """Best-effort mirror writer for SessionDB rows."""

    def __init__(self, config: PostgresMirrorConfig):
        self.config = config
        self._conn = None
        self._psycopg = None
        self._sql = None
        self._jsonb = None

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _load_psycopg(self) -> None:
        if self._psycopg is not None:
            return
        try:
            import psycopg
            from psycopg import sql
            from psycopg.types.json import Jsonb
        except ImportError as exc:
            raise RuntimeError(
                "Postgres mirror requires psycopg. Install with "
                "`pip install -e '.[postgres]'` or `pip install psycopg[binary]`."
            ) from exc
        self._psycopg = psycopg
        self._sql = sql
        self._jsonb = Jsonb

    def _connect(self):
        self._load_psycopg()
        if self._conn is not None and not getattr(self._conn, "closed", True):
            return self._conn
        self._conn = self._psycopg.connect(
            self.config.dsn,
            autocommit=True,
            connect_timeout=self.config.connect_timeout_seconds,
        )
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT set_config('statement_timeout', %s, false)",
                (f"{self.config.statement_timeout_ms}ms",),
            )
        return self._conn

    def _handle_error(self, action: str, exc: BaseException) -> None:
        self.close()
        if self.config.strict:
            raise RuntimeError(f"Postgres mirror {action} failed: {exc}") from exc
        logger.warning("Postgres mirror %s failed: %s", action, exc)

    def _table(self, name: str):
        self._load_psycopg()
        return self._sql.SQL("{}.{}").format(
            self._sql.Identifier(self.config.schema),
            self._sql.Identifier(name),
        )

    def migrate(self) -> None:
        conn = self._connect()
        sql = self._sql
        schema_ident = sql.Identifier(self.config.schema)
        with conn.cursor() as cur:
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(schema_ident))
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {}.mirror_meta (
                        key text PRIMARY KEY,
                        value text NOT NULL,
                        updated_at timestamptz NOT NULL DEFAULT now()
                    )
                    """
                ).format(schema_ident)
            )
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {}.sessions (
                        home_key text NOT NULL,
                        id text NOT NULL,
                        source text NOT NULL,
                        user_id text,
                        model text,
                        model_config jsonb,
                        system_prompt text,
                        parent_session_id text,
                        started_at double precision NOT NULL,
                        ended_at double precision,
                        end_reason text,
                        message_count integer NOT NULL DEFAULT 0,
                        tool_call_count integer NOT NULL DEFAULT 0,
                        input_tokens bigint NOT NULL DEFAULT 0,
                        output_tokens bigint NOT NULL DEFAULT 0,
                        cache_read_tokens bigint NOT NULL DEFAULT 0,
                        cache_write_tokens bigint NOT NULL DEFAULT 0,
                        reasoning_tokens bigint NOT NULL DEFAULT 0,
                        cwd text,
                        billing_provider text,
                        billing_base_url text,
                        billing_mode text,
                        estimated_cost_usd double precision,
                        actual_cost_usd double precision,
                        cost_status text,
                        cost_source text,
                        pricing_version text,
                        title text,
                        api_call_count integer NOT NULL DEFAULT 0,
                        handoff_state text,
                        handoff_platform text,
                        handoff_error text,
                        rewind_count integer NOT NULL DEFAULT 0,
                        archived boolean NOT NULL DEFAULT false,
                        raw jsonb NOT NULL,
                        mirrored_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (home_key, id)
                    )
                    """
                ).format(schema_ident)
            )
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {}.messages (
                        home_key text NOT NULL,
                        id bigint NOT NULL,
                        session_id text NOT NULL,
                        role text NOT NULL,
                        content text,
                        tool_call_id text,
                        tool_calls jsonb,
                        tool_name text,
                        timestamp double precision NOT NULL,
                        token_count integer,
                        finish_reason text,
                        reasoning text,
                        reasoning_content text,
                        reasoning_details jsonb,
                        codex_reasoning_items jsonb,
                        codex_message_items jsonb,
                        platform_message_id text,
                        observed boolean NOT NULL DEFAULT false,
                        active boolean NOT NULL DEFAULT true,
                        raw jsonb NOT NULL,
                        mirrored_at timestamptz NOT NULL DEFAULT now(),
                        PRIMARY KEY (home_key, id),
                        FOREIGN KEY (home_key, session_id)
                            REFERENCES {}.sessions (home_key, id)
                            ON DELETE CASCADE
                    )
                    """
                ).format(schema_ident, schema_ident)
            )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {}.sessions (home_key, started_at DESC)"
                ).format(sql.Identifier("idx_hermes_sessions_home_started"), schema_ident)
            )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {}.messages (home_key, session_id, id)"
                ).format(sql.Identifier("idx_hermes_messages_home_session"), schema_ident)
            )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {} ON {}.messages (home_key, timestamp)"
                ).format(sql.Identifier("idx_hermes_messages_home_timestamp"), schema_ident)
            )
            cur.execute(
                sql.SQL(
                    """
                    INSERT INTO {}.mirror_meta (key, value, updated_at)
                    VALUES ('schema_version', '1', now())
                    ON CONFLICT (key)
                    DO UPDATE SET value = EXCLUDED.value, updated_at = now()
                    """
                ).format(schema_ident)
            )

    def upsert_session(self, row: Mapping[str, Any]) -> None:
        try:
            self.migrate()
            data = {column: row.get(column) for column in SESSION_COLUMNS}
            raw = dict(row)
            data["model_config"] = _parse_json(data.get("model_config"))
            data["archived"] = bool(data.get("archived") or 0)
            values = [
                self.config.home_key,
                data["id"],
                data["source"],
                data["user_id"],
                data["model"],
                self._jsonb(data["model_config"]),
                data["system_prompt"],
                data["parent_session_id"],
                data["started_at"],
                data["ended_at"],
                data["end_reason"],
                data["message_count"] or 0,
                data["tool_call_count"] or 0,
                data["input_tokens"] or 0,
                data["output_tokens"] or 0,
                data["cache_read_tokens"] or 0,
                data["cache_write_tokens"] or 0,
                data["reasoning_tokens"] or 0,
                data["cwd"],
                data["billing_provider"],
                data["billing_base_url"],
                data["billing_mode"],
                data["estimated_cost_usd"],
                data["actual_cost_usd"],
                data["cost_status"],
                data["cost_source"],
                data["pricing_version"],
                data["title"],
                data["api_call_count"] or 0,
                data["handoff_state"],
                data["handoff_platform"],
                data["handoff_error"],
                data["rewind_count"] or 0,
                data["archived"],
                self._jsonb(raw),
            ]
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(
                    self._sql.SQL(
                        """
                        INSERT INTO {} (
                            home_key, id, source, user_id, model, model_config,
                            system_prompt, parent_session_id, started_at, ended_at,
                            end_reason, message_count, tool_call_count, input_tokens,
                            output_tokens, cache_read_tokens, cache_write_tokens,
                            reasoning_tokens, cwd, billing_provider, billing_base_url,
                            billing_mode, estimated_cost_usd, actual_cost_usd,
                            cost_status, cost_source, pricing_version, title,
                            api_call_count, handoff_state, handoff_platform,
                            handoff_error, rewind_count, archived, raw, mirrored_at
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now()
                        )
                        ON CONFLICT (home_key, id) DO UPDATE SET
                            source = EXCLUDED.source,
                            user_id = EXCLUDED.user_id,
                            model = EXCLUDED.model,
                            model_config = EXCLUDED.model_config,
                            system_prompt = EXCLUDED.system_prompt,
                            parent_session_id = EXCLUDED.parent_session_id,
                            started_at = EXCLUDED.started_at,
                            ended_at = EXCLUDED.ended_at,
                            end_reason = EXCLUDED.end_reason,
                            message_count = EXCLUDED.message_count,
                            tool_call_count = EXCLUDED.tool_call_count,
                            input_tokens = EXCLUDED.input_tokens,
                            output_tokens = EXCLUDED.output_tokens,
                            cache_read_tokens = EXCLUDED.cache_read_tokens,
                            cache_write_tokens = EXCLUDED.cache_write_tokens,
                            reasoning_tokens = EXCLUDED.reasoning_tokens,
                            cwd = EXCLUDED.cwd,
                            billing_provider = EXCLUDED.billing_provider,
                            billing_base_url = EXCLUDED.billing_base_url,
                            billing_mode = EXCLUDED.billing_mode,
                            estimated_cost_usd = EXCLUDED.estimated_cost_usd,
                            actual_cost_usd = EXCLUDED.actual_cost_usd,
                            cost_status = EXCLUDED.cost_status,
                            cost_source = EXCLUDED.cost_source,
                            pricing_version = EXCLUDED.pricing_version,
                            title = EXCLUDED.title,
                            api_call_count = EXCLUDED.api_call_count,
                            handoff_state = EXCLUDED.handoff_state,
                            handoff_platform = EXCLUDED.handoff_platform,
                            handoff_error = EXCLUDED.handoff_error,
                            rewind_count = EXCLUDED.rewind_count,
                            archived = EXCLUDED.archived,
                            raw = EXCLUDED.raw,
                            mirrored_at = now()
                        """
                    ).format(self._table("sessions")),
                    values,
                )
        except Exception as exc:
            self._handle_error("session upsert", exc)

    def upsert_message(self, row: Mapping[str, Any]) -> None:
        try:
            self.migrate()
            data = {column: row.get(column) for column in MESSAGE_COLUMNS}
            raw = dict(row)
            values = [
                self.config.home_key,
                data["id"],
                data["session_id"],
                data["role"],
                data["content"],
                data["tool_call_id"],
                self._jsonb(_parse_json(data.get("tool_calls"))),
                data["tool_name"],
                data["timestamp"],
                data["token_count"],
                data["finish_reason"],
                data["reasoning"],
                data["reasoning_content"],
                self._jsonb(_parse_json(data.get("reasoning_details"))),
                self._jsonb(_parse_json(data.get("codex_reasoning_items"))),
                self._jsonb(_parse_json(data.get("codex_message_items"))),
                data["platform_message_id"],
                bool(data.get("observed") or 0),
                bool(data.get("active", 1)),
                self._jsonb(raw),
            ]
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(
                    self._sql.SQL(
                        """
                        INSERT INTO {} (
                            home_key, id, session_id, role, content, tool_call_id,
                            tool_calls, tool_name, timestamp, token_count,
                            finish_reason, reasoning, reasoning_content,
                            reasoning_details, codex_reasoning_items,
                            codex_message_items, platform_message_id, observed,
                            active, raw, mirrored_at
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, now()
                        )
                        ON CONFLICT (home_key, id) DO UPDATE SET
                            session_id = EXCLUDED.session_id,
                            role = EXCLUDED.role,
                            content = EXCLUDED.content,
                            tool_call_id = EXCLUDED.tool_call_id,
                            tool_calls = EXCLUDED.tool_calls,
                            tool_name = EXCLUDED.tool_name,
                            timestamp = EXCLUDED.timestamp,
                            token_count = EXCLUDED.token_count,
                            finish_reason = EXCLUDED.finish_reason,
                            reasoning = EXCLUDED.reasoning,
                            reasoning_content = EXCLUDED.reasoning_content,
                            reasoning_details = EXCLUDED.reasoning_details,
                            codex_reasoning_items = EXCLUDED.codex_reasoning_items,
                            codex_message_items = EXCLUDED.codex_message_items,
                            platform_message_id = EXCLUDED.platform_message_id,
                            observed = EXCLUDED.observed,
                            active = EXCLUDED.active,
                            raw = EXCLUDED.raw,
                            mirrored_at = now()
                        """
                    ).format(self._table("messages")),
                    values,
                )
        except Exception as exc:
            self._handle_error("message upsert", exc)

    def delete_session(self, session_id: str) -> None:
        try:
            self.migrate()
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(
                    self._sql.SQL("DELETE FROM {} WHERE home_key = %s AND id = %s").format(
                        self._table("sessions")
                    ),
                    (self.config.home_key, session_id),
                )
        except Exception as exc:
            self._handle_error("session delete", exc)

    def delete_sessions(self, session_ids: Iterable[str]) -> None:
        ids = [sid for sid in session_ids if sid]
        if not ids:
            return
        for sid in ids:
            self.delete_session(sid)

    def delete_messages_for_session(self, session_id: str) -> None:
        try:
            self.migrate()
            conn = self._connect()
            with conn.cursor() as cur:
                cur.execute(
                    self._sql.SQL(
                        "DELETE FROM {} WHERE home_key = %s AND session_id = %s"
                    ).format(self._table("messages")),
                    (self.config.home_key, session_id),
                )
        except Exception as exc:
            self._handle_error("message delete", exc)

    def sqlite_counts(self, sqlite_db_path: Path) -> dict[str, int]:
        conn = sqlite3.connect(str(sqlite_db_path))
        try:
            if not _sqlite_table_exists(conn, "sessions"):
                return {"sessions": 0, "messages": 0}
            return {
                "sessions": int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]),
                "messages": int(conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]),
            }
        finally:
            conn.close()

    def postgres_counts(self) -> dict[str, int]:
        self.migrate()
        conn = self._connect()
        with conn.cursor() as cur:
            cur.execute(
                self._sql.SQL("SELECT COUNT(*) FROM {} WHERE home_key = %s").format(
                    self._table("sessions")
                ),
                (self.config.home_key,),
            )
            sessions = int(cur.fetchone()[0])
            cur.execute(
                self._sql.SQL("SELECT COUNT(*) FROM {} WHERE home_key = %s").format(
                    self._table("messages")
                ),
                (self.config.home_key,),
            )
            messages = int(cur.fetchone()[0])
        return {"sessions": sessions, "messages": messages}

    def backfill_from_sqlite(self, sqlite_db_path: Path) -> dict[str, int]:
        self.migrate()
        conn = sqlite3.connect(str(sqlite_db_path))
        conn.row_factory = sqlite3.Row
        try:
            if not _sqlite_table_exists(conn, "sessions"):
                return {"sessions": 0, "messages": 0}
            sessions = conn.execute("SELECT * FROM sessions ORDER BY started_at, id").fetchall()
            messages = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
            for row in sessions:
                self.upsert_session(_row_to_dict(row, SESSION_COLUMNS))
            for row in messages:
                self.upsert_message(_row_to_dict(row, MESSAGE_COLUMNS))
            return {"sessions": len(sessions), "messages": len(messages)}
        finally:
            conn.close()

    def sync_session_by_id(self, sqlite_conn: sqlite3.Connection, session_id: str) -> None:
        if not self.enabled:
            return
        row = sqlite_conn.execute(
            "SELECT * FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is not None:
            self.upsert_session(_row_to_dict(row, SESSION_COLUMNS))

    def sync_message_by_id(self, sqlite_conn: sqlite3.Connection, message_id: int) -> None:
        if not self.enabled:
            return
        row = sqlite_conn.execute(
            "SELECT * FROM messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        if row is not None:
            msg = _row_to_dict(row, MESSAGE_COLUMNS)
            self.sync_session_by_id(sqlite_conn, str(msg["session_id"]))
            self.upsert_message(msg)

    def sync_session_with_messages(self, sqlite_conn: sqlite3.Connection, session_id: str) -> None:
        if not self.enabled:
            return
        self.sync_session_by_id(sqlite_conn, session_id)
        self.delete_messages_for_session(session_id)
        rows = sqlite_conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        for row in rows:
            self.upsert_message(_row_to_dict(row, MESSAGE_COLUMNS))


_cached_mirror: Optional[PostgresMirror] = None
_cached_key: Optional[tuple[Any, ...]] = None


def get_postgres_mirror(*, db_path: Path | None = None) -> PostgresMirror:
    global _cached_key, _cached_mirror
    config = PostgresMirrorConfig.from_env(db_path=db_path)
    key = (
        config.enabled,
        config.dsn,
        config.schema,
        config.home_key,
        config.strict,
        config.connect_timeout_seconds,
        config.statement_timeout_ms,
    )
    if _cached_mirror is None or _cached_key != key:
        if _cached_mirror is not None:
            _cached_mirror.close()
        _cached_mirror = PostgresMirror(config)
        _cached_key = key
    return _cached_mirror


def _default_sqlite_path() -> Path:
    return get_hermes_home() / "state.db"


def _print_json(payload: Mapping[str, Any]) -> None:
    print(_json_dumps(payload))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hermes-postgres-mirror",
        description="Manage the optional PostgreSQL mirror for Hermes state.db.",
    )
    parser.add_argument(
        "--sqlite-db",
        type=Path,
        default=_default_sqlite_path(),
        help="Path to Hermes state.db. Defaults to $HERMES_HOME/state.db.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="Create or update Postgres mirror tables.")
    subparsers.add_parser("backfill", help="Copy current SQLite sessions/messages to Postgres.")
    subparsers.add_parser("status", help="Compare SQLite and Postgres row counts.")

    args = parser.parse_args(argv)
    mirror = get_postgres_mirror(db_path=args.sqlite_db)
    started = time.time()
    if args.command == "migrate":
        mirror.migrate()
        _print_json(
            {
                "ok": True,
                "command": "migrate",
                "schema": mirror.config.schema,
                "homeKey": mirror.config.home_key,
                "elapsedMs": int((time.time() - started) * 1000),
            }
        )
        return 0
    if args.command == "backfill":
        counts = mirror.backfill_from_sqlite(args.sqlite_db)
        _print_json(
            {
                "ok": True,
                "command": "backfill",
                "schema": mirror.config.schema,
                "homeKey": mirror.config.home_key,
                **counts,
                "elapsedMs": int((time.time() - started) * 1000),
            }
        )
        return 0
    if args.command == "status":
        sqlite_counts = mirror.sqlite_counts(args.sqlite_db)
        postgres_counts = mirror.postgres_counts()
        ok = sqlite_counts == postgres_counts
        _print_json(
            {
                "ok": ok,
                "command": "status",
                "schema": mirror.config.schema,
                "homeKey": mirror.config.home_key,
                "sqlite": sqlite_counts,
                "postgres": postgres_counts,
                "elapsedMs": int((time.time() - started) * 1000),
            }
        )
        return 0 if ok else 1
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
