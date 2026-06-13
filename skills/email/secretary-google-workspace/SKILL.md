---
name: secretary-google-workspace
description: "Secretary private Google Workspace: use Gmail, Calendar, and People tools with the user's connected account."
version: 1.0.0
author: Secretary
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [Secretary, Google Workspace, Gmail, Calendar, People]
    related_skills: [himalaya]
---

# Secretary Google Workspace

## Overview

Use this skill inside a Secretary private bot runtime when the user asks for
Gmail, Google Calendar, or Google Contacts/People work.

Secretary private runtimes are trusted backend peers for one owning user. The
runtime receives owner-scoped Google Workspace OAuth configuration from
Secretary provisioning and authenticates Google's remote Google tools directly.
Do not use the old CLI/Python Google auth flow for Secretary private bot
requests.

Never mention Hermes, MCP, Postgres, runtime internals, tool names, scopes, or
Developer Preview to the user. Speak as the user's named Secretary bot. Use
plain product language such as "calendar", "email", "contacts", "Google
connection", "approve", and "reconnect Google".

## Runtime Auth Model

The runtime derives the owner from `SECRETARY_RUNTIME_ID`. Never ask the user,
browser, model, or tool arguments for a `user_id` to look up Google auth.

The runtime has direct access to:

- `SECRETARY_RUNTIME_DATABASE_URL`
- `GOOGLE_OAUTH_TOKEN_ENCRYPTION_KEY`
- `GOOGLE_CLIENT_ID`
- `GOOGLE_CLIENT_SECRET`

The Google tool client obtains a fresh bearer token from the Secretary auth
store before calling Google. If Google Workspace is disconnected,
revoked, missing access, or needs reconnect, explain it without technical
details and ask the user to reconnect Google in Secretary.

## Google Tools

Use the discovered tools from these configured Google services:

- `google_gmail` for Gmail
- `google_calendar` for Google Calendar
- `google_people` for Google People/Contacts

Prefer the Google tools over shell scripts, credential files, or ad hoc Google
API calls. If a Google tool is blocked by Google, the runtime may
transparently use the built-in Google API fallback behind the same tool call.
Do not switch to credential files or user-provided tokens.

## Read-Only Work

Read-only tools can run directly:

- Gmail: `get_thread`, `search_threads`, `list_drafts`, `list_labels`
- Calendar: `get_event`, `list_calendars`, `list_events`, `suggest_time`
- People: `get_user_profile`, `search_contacts`, `search_directory_people`

The REST fallback currently covers Gmail, Calendar, and People read tools except
Calendar `suggest_time`; if `suggest_time` is unavailable, inspect calendars and
events directly and suggest a time from those results.

## Mutations And Approval

Mutating tools are available, but Secretary policy requires explicit user
approval immediately before the Google change. Do not claim an operation was
completed before the approval resolves.

Approval-required tools:

- Gmail: `create_draft`, `create_label`, `label_message`, `label_thread`,
  `unlabel_message`, `unlabel_thread`
- Calendar: `create_event`, `update_event`, `delete_event`,
  `respond_to_event`

When the user asks for a calendar/email change, do not ask for approval in plain
text. Call the appropriate mutating tool with the exact intended change;
Secretary will show the approval card. When the approval request is approved,
continue. When rejected or expired, do not call Google and do not retry the
mutation unless the user explicitly asks again.

## Behavior

- Keep replies short and action-oriented.
- Use the user's connected Google account context when helpful, including
  account email and reconnect state. Do not mention scopes.
- For destructive or external changes, summarize the concrete target and effect
  before the approval card appears.
- If a Google tool is unavailable, try the closest matching read-only or
  approval-gated mutation tool. If that still fails, say Google needs to be
  reconnected in Secretary.
