from __future__ import annotations

import sys

import pytest

import postgres_mirror
from hermes_state import SessionDB


class FakeMirror:
    enabled = True

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple[str, object]] = []

    def _record(self, name: str, value: object) -> None:
        if self.fail:
            raise RuntimeError("mirror down")
        self.calls.append((name, value))

    def sync_session_by_id(self, _sqlite_conn, session_id: str) -> None:
        self._record("sync_session", session_id)

    def sync_message_by_id(self, _sqlite_conn, message_id: int) -> None:
        self._record("sync_message", message_id)

    def sync_session_with_messages(self, _sqlite_conn, session_id: str) -> None:
        self._record("sync_transcript", session_id)

    def delete_session(self, session_id: str) -> None:
        self._record("delete_session", session_id)

    def delete_sessions(self, session_ids) -> None:
        self._record("delete_sessions", tuple(session_ids))


def test_disabled_postgres_mirror_keeps_sqlite_canonical(tmp_path, monkeypatch):
    """The mirror is optional and must not require psycopg when disabled."""
    monkeypatch.setenv("HERMES_POSTGRES_MIRROR_ENABLED", "false")
    monkeypatch.setitem(sys.modules, "psycopg", None)

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = db.create_session("s-disabled", "api_server", model="test-model")
        msg_id = db.append_message(sid, role="user", content="hello")

        assert msg_id == 1
        assert db.get_session(sid)["message_count"] == 1
        assert db.get_messages(sid)[0]["content"] == "hello"
    finally:
        db.close()


def test_sessiondb_mirror_hooks_cover_core_writes(tmp_path, monkeypatch):
    mirror = FakeMirror()
    monkeypatch.setattr(postgres_mirror, "get_postgres_mirror", lambda **_: mirror)

    db = SessionDB(tmp_path / "state.db")
    try:
        sid = db.create_session("s-hooks", "api_server", model="test-model")
        db.append_message(sid, role="user", content="hello")
        db.set_session_title(sid, "Hook smoke")
        db.replace_messages(
            sid,
            [
                {"role": "user", "content": "replacement"},
                {"role": "assistant", "content": "ok"},
            ],
        )
        db.clear_messages(sid)
        db.delete_session(sid)
    finally:
        db.close()

    assert ("sync_session", "s-hooks") in mirror.calls
    assert ("sync_message", 1) in mirror.calls
    assert mirror.calls.count(("sync_transcript", "s-hooks")) == 2
    assert ("delete_session", "s-hooks") in mirror.calls


def test_mirror_failure_is_best_effort_by_default(tmp_path, monkeypatch, caplog):
    mirror = FakeMirror(fail=True)
    monkeypatch.setattr(postgres_mirror, "get_postgres_mirror", lambda **_: mirror)

    db = SessionDB(tmp_path / "state.db")
    try:
        with caplog.at_level("WARNING", logger="hermes_state"):
            sid = db.create_session("s-best-effort", "api_server")

        assert db.get_session(sid) is not None
        assert "Postgres mirror session sync failed" in caplog.text
    finally:
        db.close()


def test_strict_mirror_failure_surfaces_after_sqlite_commit(tmp_path, monkeypatch):
    mirror = FakeMirror(fail=True)
    monkeypatch.setenv("HERMES_POSTGRES_STRICT", "true")
    monkeypatch.setattr(postgres_mirror, "get_postgres_mirror", lambda **_: mirror)

    db = SessionDB(tmp_path / "state.db")
    try:
        with pytest.raises(RuntimeError, match="mirror down"):
            db.create_session("s-strict", "api_server")

        assert db.get_session("s-strict") is not None
    finally:
        db.close()


def test_postgres_mirror_config_rejects_unsafe_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_POSTGRES_SCHEMA", "bad-name")

    with pytest.raises(ValueError, match="simple PostgreSQL identifier"):
        postgres_mirror.PostgresMirrorConfig.from_env(db_path=tmp_path / "state.db")
