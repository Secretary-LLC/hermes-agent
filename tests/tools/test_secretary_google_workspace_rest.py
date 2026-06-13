import asyncio
import json


def test_google_mcp_error_allows_rest_fallback():
    from tools.secretary_google_workspace_rest import (
        google_mcp_error_allows_rest_fallback,
    )

    assert google_mcp_error_allows_rest_fallback(
        {"error": "The caller does not have permission"}
    )
    assert not google_mcp_error_allows_rest_fallback({"error": "bad request"})


def test_google_rest_fallback_support_includes_approved_calendar_writes():
    from tools.secretary_google_workspace_rest import google_rest_fallback_supported

    auth_config = {
        "type": "secretary_google_workspace_postgres",
        "service": "calendar",
    }

    assert google_rest_fallback_supported(auth_config, "list_events")
    assert google_rest_fallback_supported(auth_config, "update_event")
    assert not google_rest_fallback_supported(auth_config, "create_draft")


def test_calendar_list_events_rest_fallback_maps_args(monkeypatch):
    import tools.secretary_google_workspace_rest as rest

    captured = {}

    async def fake_google_json(service, method, url, *, params=None, json_body=None):
        captured.update(
            {
                "service": service,
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
            }
        )
        return {"items": [{"summary": "Planning"}]}

    monkeypatch.setattr(rest, "_google_json", fake_google_json)

    result = asyncio.run(
        rest.execute_google_workspace_rest_fallback(
            server_name="google_calendar",
            tool_name="list_events",
            args={
                "calendarId": "primary",
                "timeMin": "2026-06-14T00:00:00+08:00",
                "timeMax": "2026-06-15T00:00:00+08:00",
                "pageSize": "5",
            },
            auth_config={
                "type": "secretary_google_workspace_postgres",
                "service": "calendar",
            },
        )
    )

    payload = json.loads(result)
    assert payload["source"] == "google_api"
    assert payload["result"]["items"][0]["summary"] == "Planning"
    assert captured["service"] == "calendar"
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/calendars/primary/events")
    assert captured["params"]["timeMin"] == "2026-06-14T00:00:00+08:00"
    assert captured["params"]["timeMax"] == "2026-06-15T00:00:00+08:00"
    assert captured["params"]["maxResults"] == 5
    assert captured["params"]["singleEvents"] is True


def test_calendar_date_only_bounds_become_rfc3339_with_timezone(monkeypatch):
    import tools.secretary_google_workspace_rest as rest

    captured = {}

    async def fake_google_json(service, method, url, *, params=None, json_body=None):
        captured["params"] = params
        return {"items": []}

    monkeypatch.setattr(rest, "_google_json", fake_google_json)

    asyncio.run(
        rest.execute_google_workspace_rest_fallback(
            server_name="google_calendar",
            tool_name="list_events",
            args={
                "calendarId": "primary",
                "timeMin": "2026-06-14",
                "timeMax": "2026-06-15",
                "timeZone": "Asia/Manila",
            },
            auth_config={
                "type": "secretary_google_workspace_postgres",
                "service": "calendar",
            },
        )
    )

    assert captured["params"]["timeMin"] == "2026-06-14T00:00:00+08:00"
    assert captured["params"]["timeMax"] == "2026-06-15T00:00:00+08:00"


def test_calendar_update_event_rest_fallback_maps_patch_body(monkeypatch):
    import tools.secretary_google_workspace_rest as rest

    captured = {}

    async def fake_google_json(service, method, url, *, params=None, json_body=None):
        captured.update(
            {
                "service": service,
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
            }
        )
        return {"id": "event_123", "summary": "TEST"}

    monkeypatch.setattr(rest, "_google_json", fake_google_json)

    result = asyncio.run(
        rest.execute_google_workspace_rest_fallback(
            server_name="google_calendar",
            tool_name="update_event",
            args={
                "calendarId": "primary",
                "eventId": "event_123",
                "summary": "TEST",
                "startTime": "2026-06-14T16:30:00",
                "endTime": "2026-06-14T17:30:00",
                "timeZone": "Asia/Manila",
                "sendUpdates": "all",
            },
            auth_config={
                "type": "secretary_google_workspace_postgres",
                "service": "calendar",
            },
        )
    )

    payload = json.loads(result)
    assert payload["source"] == "google_api"
    assert captured["service"] == "calendar"
    assert captured["method"] == "PATCH"
    assert captured["url"].endswith("/calendars/primary/events/event_123")
    assert captured["params"]["sendUpdates"] == "all"
    assert captured["json_body"] == {
        "summary": "TEST",
        "start": {
            "dateTime": "2026-06-14T16:30:00",
            "timeZone": "Asia/Manila",
        },
        "end": {
            "dateTime": "2026-06-14T17:30:00",
            "timeZone": "Asia/Manila",
        },
    }
