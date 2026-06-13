"""Google Workspace REST fallback for Secretary private Hermes runtimes.

Google's Workspace MCP endpoints are still preview-gated in some projects:
tool discovery can work while actual tool calls return permission denied.  For
Secretary private runtimes, keep the Hermes MCP tool surface intact and fall
back to regular Google REST APIs for read-only Workspace tools when that
specific MCP failure occurs.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from tools.secretary_google_workspace import (
    get_auth_store,
    is_secretary_google_workspace_auth,
    service_from_auth_config,
)

logger = logging.getLogger(__name__)

GOOGLE_API_TIMEOUT = 30.0

DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_WITHOUT_OFFSET_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ][0-2]\d:[0-5]\d(?::[0-5]\d(?:\.\d+)?)?$"
)

GOOGLE_MCP_PERMISSION_DENIED_MARKERS = (
    "the caller does not have permission",
    "permission_denied",
    "permission denied",
    "403 forbidden",
    "http 403",
)

REST_FALLBACK_TOOLS = {
    "gmail": {
        "get_thread",
        "search_threads",
        "list_drafts",
        "list_labels",
    },
    "calendar": {
        "get_event",
        "list_calendars",
        "list_events",
        "create_event",
        "update_event",
        "delete_event",
        "respond_to_event",
    },
    "people": {
        "get_user_profile",
        "search_contacts",
        "search_directory_people",
    },
}


def google_mcp_error_allows_rest_fallback(payload: Any) -> bool:
    """Return True when a Google MCP failure is the preview permission gate."""

    if isinstance(payload, dict):
        value = payload.get("error") or payload.get("message") or payload
    else:
        value = payload
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value or "")
    lowered = text.lower()
    return any(marker in lowered for marker in GOOGLE_MCP_PERMISSION_DENIED_MARKERS)


def google_rest_fallback_supported(auth_config: Any, tool_name: str) -> bool:
    if not is_secretary_google_workspace_auth(auth_config):
        return False
    service = service_from_auth_config(auth_config)
    if not service:
        return False
    return tool_name in REST_FALLBACK_TOOLS.get(service, set())


async def execute_google_workspace_rest_fallback(
    *,
    server_name: str,
    tool_name: str,
    args: dict[str, Any],
    auth_config: Any,
) -> str | None:
    """Execute a Google Workspace tool with regular Google REST.

    Returns a JSON tool result string, or None when this tool/service should not
    fall back. Mutating tools only reach this function after the MCP tool
    handler has already completed Secretary's approval gate.
    """

    if not google_rest_fallback_supported(auth_config, tool_name):
        return None
    service = service_from_auth_config(auth_config)
    if not service:
        return None
    safe_args = args if isinstance(args, dict) else {}

    try:
        if service == "calendar":
            payload = await _execute_calendar(tool_name, safe_args)
        elif service == "gmail":
            payload = await _execute_gmail(tool_name, safe_args)
        elif service == "people":
            payload = await _execute_people(tool_name, safe_args)
        else:
            return None
    except Exception as exc:
        logger.warning(
            "Google REST fallback failed for %s/%s: %s",
            server_name,
            tool_name,
            exc,
        )
        return json.dumps(
            {
                "error": (
                    "Google did not allow that change. Please reconnect Google "
                    "in Secretary, then try again."
                )
            },
            ensure_ascii=False,
        )

    return json.dumps(
        {
            "result": payload,
            "source": "google_api",
            "fallbackReason": "google_action_retried",
        },
        ensure_ascii=False,
    )


async def _execute_calendar(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "list_calendars":
        params = _compact_params(
            {
                "maxResults": _int_arg(args, "maxResults", "max_results", "pageSize", "page_size", "limit"),
                "pageToken": _arg(args, "pageToken", "page_token"),
                "showDeleted": _bool_arg(args, "showDeleted", "show_deleted"),
                "showHidden": _bool_arg(args, "showHidden", "show_hidden"),
            }
        )
        return await _google_json(
            "calendar",
            "GET",
            "https://www.googleapis.com/calendar/v3/users/me/calendarList",
            params=params,
        )

    calendar_id = _calendar_id(args)
    if tool_name == "get_event":
        event_id = _arg(args, "eventId", "event_id", "id")
        if not event_id:
            raise ValueError("Missing required event id.")
        return await _google_json(
            "calendar",
            "GET",
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
        )

    if tool_name == "list_events":
        order_by = _arg(args, "orderBy", "order_by", default="startTime")
        rest_order_by = "updated" if order_by == "lastModified" else "startTime"
        requested_time_zone = _arg(args, "timeZone", "time_zone")
        params = _compact_params(
            {
                "timeMin": _normalize_calendar_time_bound(
                    _arg(args, "timeMin", "time_min", "startTime", "start_time", "start", "from"),
                    requested_time_zone,
                ),
                "timeMax": _normalize_calendar_time_bound(
                    _arg(args, "timeMax", "time_max", "endTime", "end_time", "end", "to"),
                    requested_time_zone,
                ),
                "maxResults": _int_arg(args, "maxResults", "max_results", "pageSize", "page_size", "limit", default=20),
                "pageToken": _arg(args, "pageToken", "page_token"),
                "q": _arg(args, "q", "query", "search", "fullText", "full_text"),
                "timeZone": requested_time_zone,
                "singleEvents": True,
                "showDeleted": False,
                "orderBy": rest_order_by,
            }
        )
        data = await _google_json(
            "calendar",
            "GET",
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
            params=params,
        )
        if order_by == "startTimeDesc" and isinstance(data.get("items"), list):
            data["items"] = sorted(
                data["items"],
                key=lambda item: (
                    ((item.get("start") or {}).get("dateTime"))
                    or ((item.get("start") or {}).get("date"))
                    or ""
                ),
                reverse=True,
            )
        return data

    if tool_name == "create_event":
        calendar_id = _calendar_id_for_event_args(args)
        body = _calendar_event_body(args)
        if not body:
            raise ValueError("Missing calendar event details.")
        return await _google_json(
            "calendar",
            "POST",
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
            params=_calendar_write_params(args),
            json_body=body,
        )

    if tool_name == "update_event":
        calendar_id = _calendar_id_for_event_args(args)
        event_id = _event_id(args)
        if not event_id:
            raise ValueError("Missing calendar event id.")
        body = _calendar_event_body(args)
        if not body:
            raise ValueError("Missing calendar event changes.")
        if "start" in body and "end" not in body:
            existing = await _google_json(
                "calendar",
                "GET",
                f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
            )
            inferred_end = _shift_existing_end(existing, body["start"])
            if inferred_end:
                body["end"] = inferred_end
        return await _google_json(
            "calendar",
            "PATCH",
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
            params=_calendar_write_params(args),
            json_body=body,
        )

    if tool_name == "delete_event":
        calendar_id = _calendar_id_for_event_args(args)
        event_id = _event_id(args)
        if not event_id:
            raise ValueError("Missing calendar event id.")
        return await _google_json(
            "calendar",
            "DELETE",
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
            params=_calendar_write_params(args),
        )

    if tool_name == "respond_to_event":
        calendar_id = _calendar_id_for_event_args(args)
        event_id = _event_id(args)
        response_status = str(
            _arg(args, "responseStatus", "response_status", "status", "response")
            or ""
        ).strip().lower()
        status_map = {
            "accept": "accepted",
            "accepted": "accepted",
            "yes": "accepted",
            "tentative": "tentative",
            "maybe": "tentative",
            "decline": "declined",
            "declined": "declined",
            "no": "declined",
        }
        response_status = status_map.get(response_status, response_status)
        if not event_id or response_status not in {"accepted", "tentative", "declined"}:
            raise ValueError("Missing calendar response details.")
        existing = await _google_json(
            "calendar",
            "GET",
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
        )
        attendees = existing.get("attendees")
        if not isinstance(attendees, list):
            attendees = []
        target_email = str(
            _arg(args, "email", "attendeeEmail", "attendee_email", default="")
            or ""
        ).strip().lower()
        updated = False
        next_attendees = []
        for attendee in attendees:
            if not isinstance(attendee, dict):
                continue
            attendee_email = str(attendee.get("email") or "").strip().lower()
            if attendee.get("self") or (target_email and attendee_email == target_email):
                attendee = {**attendee, "responseStatus": response_status}
                updated = True
            next_attendees.append(attendee)
        if not updated and target_email:
            next_attendees.append(
                {"email": target_email, "responseStatus": response_status}
            )
        if not next_attendees:
            raise ValueError("This calendar event has no attendee to update.")
        return await _google_json(
            "calendar",
            "PATCH",
            f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events/{event_id}",
            params=_calendar_write_params(args),
            json_body={"attendees": next_attendees},
        )

    raise ValueError(f"Unsupported Calendar REST fallback tool: {tool_name}")


async def _execute_gmail(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    base = "https://gmail.googleapis.com/gmail/v1/users/me"

    if tool_name == "list_labels":
        return await _google_json("gmail", "GET", f"{base}/labels")

    if tool_name == "list_drafts":
        params = _compact_params(
            {
                "maxResults": _int_arg(args, "maxResults", "max_results", "pageSize", "page_size", "limit", default=20),
                "pageToken": _arg(args, "pageToken", "page_token"),
            }
        )
        data = await _google_json("gmail", "GET", f"{base}/drafts", params=params)
        drafts = data.get("drafts") if isinstance(data, dict) else None
        if isinstance(drafts, list):
            data["drafts"] = await _hydrate_gmail_drafts(drafts[:10])
        return data

    if tool_name == "search_threads":
        params = _compact_params(
            {
                "q": _arg(args, "q", "query", "search", "gmailQuery", "gmail_query"),
                "maxResults": _int_arg(args, "maxResults", "max_results", "pageSize", "page_size", "limit", default=10),
                "pageToken": _arg(args, "pageToken", "page_token"),
                "labelIds": _arg(args, "labelIds", "label_ids"),
                "includeSpamTrash": _bool_arg(args, "includeSpamTrash", "include_spam_trash"),
            }
        )
        data = await _google_json("gmail", "GET", f"{base}/threads", params=params)
        threads = data.get("threads") if isinstance(data, dict) else None
        if isinstance(threads, list):
            data["threads"] = await _hydrate_gmail_threads(threads[:10])
        return data

    if tool_name == "get_thread":
        thread_id = _arg(args, "threadId", "thread_id", "id")
        if not thread_id:
            raise ValueError("Missing required thread id.")
        return await _gmail_thread(thread_id)

    raise ValueError(f"Unsupported Gmail REST fallback tool: {tool_name}")


async def _execute_people(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    if tool_name == "get_user_profile":
        params = {
            "personFields": _arg(
                args,
                "personFields",
                "person_fields",
                "readMask",
                "read_mask",
                default="names,emailAddresses,photos,organizations",
            )
        }
        return await _google_json(
            "people",
            "GET",
            "https://people.googleapis.com/v1/people/me",
            params=params,
        )

    if tool_name == "search_contacts":
        query = _arg(args, "query", "q", "search")
        if not query:
            raise ValueError("Missing required contacts search query.")
        params = _compact_params(
            {
                "query": query,
                "pageSize": _int_arg(args, "pageSize", "page_size", "maxResults", "max_results", "limit", default=10),
                "readMask": _arg(
                    args,
                    "readMask",
                    "read_mask",
                    default="names,emailAddresses,phoneNumbers,organizations,photos",
                ),
            }
        )
        return await _google_json(
            "people",
            "GET",
            "https://people.googleapis.com/v1/people:searchContacts",
            params=params,
        )

    if tool_name == "search_directory_people":
        query = _arg(args, "query", "q", "search")
        if not query:
            raise ValueError("Missing required directory search query.")
        params = _compact_params(
            {
                "query": query,
                "pageSize": _int_arg(args, "pageSize", "page_size", "maxResults", "max_results", "limit", default=10),
                "readMask": _arg(
                    args,
                    "readMask",
                    "read_mask",
                    default="names,emailAddresses,phoneNumbers,organizations,photos",
                ),
                "sources": _arg(
                    args,
                    "sources",
                    "source",
                    default="DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
                ),
            }
        )
        return await _google_json(
            "people",
            "GET",
            "https://people.googleapis.com/v1/people:searchDirectoryPeople",
            params=params,
        )

    raise ValueError(f"Unsupported People REST fallback tool: {tool_name}")


async def _hydrate_gmail_threads(threads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hydrated = []
    for thread in threads:
        thread_id = thread.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            hydrated.append(thread)
            continue
        hydrated.append(await _gmail_thread(thread_id))
    return hydrated


async def _hydrate_gmail_drafts(drafts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hydrated = []
    for draft in drafts:
        draft_id = draft.get("id")
        if not isinstance(draft_id, str) or not draft_id:
            hydrated.append(draft)
            continue
        hydrated.append(
            await _google_json(
                "gmail",
                "GET",
                f"https://gmail.googleapis.com/gmail/v1/users/me/drafts/{draft_id}",
                params={"format": "metadata"},
            )
        )
    return hydrated


async def _gmail_thread(thread_id: str) -> dict[str, Any]:
    return await _google_json(
        "gmail",
        "GET",
        f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{thread_id}",
        params={"format": "metadata"},
    )


async def _google_json(
    service: str,
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = await get_auth_store().get_access_token(service)
    headers = {"Authorization": f"Bearer {token.access_token}"}
    async with httpx.AsyncClient(timeout=GOOGLE_API_TIMEOUT) as client:
        response = await client.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
        )
        if response.status_code in {401, 403}:
            get_auth_store().invalidate(service)
            token = await get_auth_store().get_access_token(service, force_refresh=True)
            headers = {"Authorization": f"Bearer {token.access_token}"}
            response = await client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
            )
    if response.status_code >= 400:
        raise RuntimeError(_google_error_message(response))
    if not response.content:
        return {}
    payload = response.json()
    return payload if isinstance(payload, dict) else {"value": payload}


def _google_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = response.text
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return f"Google API {response.status_code}: {message}"
        message = payload.get("message")
        if isinstance(message, str) and message:
            return f"Google API {response.status_code}: {message}"
    return f"Google API {response.status_code}: {str(payload)[:300]}"


def _format_rest_error(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _calendar_id(args: dict[str, Any]) -> str:
    value = _arg(args, "calendarId", "calendar_id", "calendar", "id", default="primary")
    return str(value or "primary")


def _calendar_id_for_event_args(args: dict[str, Any]) -> str:
    value = _arg(args, "calendarId", "calendar_id", "calendar", default="primary")
    return str(value or "primary")


def _event_id(args: dict[str, Any]) -> str:
    value = _arg(args, "eventId", "event_id", "id")
    if not value:
        nested = _dict_arg(args, "event", "eventData", "event_data", "resource", "body")
        value = _arg(nested or {}, "eventId", "event_id", "id")
    return str(value or "")


def _calendar_write_params(args: dict[str, Any]) -> dict[str, Any]:
    return _compact_params(
        {
            "sendUpdates": _arg(
                args,
                "sendUpdates",
                "send_updates",
                default="all" if _bool_arg(args, "notifyAttendees", "notify_attendees") else None,
            ),
            "conferenceDataVersion": _int_arg(
                args,
                "conferenceDataVersion",
                "conference_data_version",
            ),
        }
    )


def _calendar_event_body(args: dict[str, Any]) -> dict[str, Any]:
    nested = _dict_arg(args, "event", "eventData", "event_data", "resource", "body")
    source = dict(nested or {})
    for key, value in args.items():
        source.setdefault(key, value)

    requested_time_zone = _arg(
        source,
        "timeZone",
        "time_zone",
        "timezone",
    )
    body: dict[str, Any] = {}

    summary = _arg(source, "summary", "title", "name")
    if summary:
        body["summary"] = str(summary)

    description = _arg(source, "description", "notes")
    if description:
        body["description"] = str(description)

    location = _arg(source, "location")
    if location:
        body["location"] = str(location)

    start = _arg(
        source,
        "start",
        "startTime",
        "start_time",
        "startDateTime",
        "start_date_time",
    )
    if start:
        body["start"] = _calendar_datetime_resource(start, requested_time_zone)

    end = _arg(
        source,
        "end",
        "endTime",
        "end_time",
        "endDateTime",
        "end_date_time",
    )
    if end:
        body["end"] = _calendar_datetime_resource(end, requested_time_zone)

    attendees = _arg(source, "attendees", "guests")
    if isinstance(attendees, list):
        body["attendees"] = attendees

    recurrence = _arg(source, "recurrence")
    if isinstance(recurrence, list):
        body["recurrence"] = recurrence

    reminders = _dict_arg(source, "reminders")
    if reminders:
        body["reminders"] = reminders

    visibility = _arg(source, "visibility")
    if visibility:
        body["visibility"] = str(visibility)

    transparency = _arg(source, "transparency")
    if transparency:
        body["transparency"] = str(transparency)

    return body


def _calendar_datetime_resource(value: Any, time_zone: Any = None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError("Calendar time must be a string or object.")
    text = value.strip().replace(" ", "T")
    if not text:
        raise ValueError("Calendar time is empty.")
    if DATE_ONLY_RE.match(text):
        return {"date": text}
    if DATETIME_WITHOUT_OFFSET_RE.match(text):
        tz = str(time_zone).strip() if isinstance(time_zone, str) and time_zone.strip() else ""
        if tz:
            return {"dateTime": text, "timeZone": tz}
        return {"dateTime": f"{text}Z"}
    return {"dateTime": text}


def _shift_existing_end(existing: dict[str, Any], next_start: Any) -> dict[str, Any] | None:
    existing_start = _parse_calendar_resource_time(existing.get("start"))
    existing_end = _parse_calendar_resource_time(existing.get("end"))
    parsed_next_start = _parse_calendar_resource_time(next_start)
    if not existing_start or not existing_end or not parsed_next_start:
        return None
    duration = existing_end - existing_start
    if duration.total_seconds() <= 0:
        return None
    next_end = parsed_next_start + duration
    if isinstance(next_start, dict) and next_start.get("timeZone"):
        return {
            "dateTime": next_end.replace(tzinfo=None).isoformat(),
            "timeZone": str(next_start["timeZone"]),
        }
    return {"dateTime": next_end.isoformat()}


def _parse_calendar_resource_time(value: Any) -> datetime | None:
    if isinstance(value, dict):
        date_time = value.get("dateTime")
        if isinstance(date_time, str) and date_time.strip():
            tz = value.get("timeZone")
            return _parse_datetime_text(date_time, tz)
        date_text = value.get("date")
        if isinstance(date_text, str) and DATE_ONLY_RE.match(date_text):
            return datetime.combine(
                date.fromisoformat(date_text),
                time.min,
                tzinfo=timezone.utc,
            )
        return None
    if isinstance(value, str):
        return _parse_datetime_text(value, None)
    return None


def _parse_datetime_text(value: str, time_zone: Any = None) -> datetime | None:
    text = value.strip().replace(" ", "T")
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_zoneinfo_or_utc(time_zone))
    return parsed


def _normalize_calendar_time_bound(value: Any, time_zone: Any = None) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return None
    if DATE_ONLY_RE.match(text):
        parsed_date = date.fromisoformat(text)
        tzinfo = _zoneinfo_or_utc(time_zone)
        return datetime.combine(parsed_date, time.min, tzinfo=tzinfo).isoformat()
    if DATETIME_WITHOUT_OFFSET_RE.match(text):
        return f"{text.replace(' ', 'T')}Z"
    return text


def _zoneinfo_or_utc(value: Any):
    if isinstance(value, str) and value.strip():
        try:
            return ZoneInfo(value.strip())
        except ZoneInfoNotFoundError:
            pass
    return timezone.utc


def _arg(args: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in args and args[name] not in (None, ""):
            return args[name]
    return default


def _dict_arg(args: dict[str, Any], *names: str) -> dict[str, Any] | None:
    value = _arg(args, *names)
    return value if isinstance(value, dict) else None


def _int_arg(args: dict[str, Any], *names: str, default: int | None = None) -> int | None:
    value = _arg(args, *names, default=default)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_arg(args: dict[str, Any], *names: str, default: bool | None = None) -> bool | None:
    value = _arg(args, *names, default=default)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y"}:
            return True
        if lowered in {"0", "false", "no", "n"}:
            return False
    return default


def _compact_params(params: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in params.items() if value not in (None, "")}
