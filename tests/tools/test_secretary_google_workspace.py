from tools.secretary_google_workspace import (
    auth_type_from_config,
    build_google_mcp_approval_data,
    decrypt_workspace_token,
    encrypt_workspace_token,
    google_mcp_tool_requires_approval,
    service_from_auth_config,
)


def test_workspace_token_crypto_roundtrips_node_format(monkeypatch):
    monkeypatch.setenv(
        "GOOGLE_OAUTH_TOKEN_ENCRYPTION_KEY",
        "secret-key-with-at-least-32-characters",
    )

    payload = encrypt_workspace_token("ya29.test-token")

    assert payload.count(".") == 2
    assert decrypt_workspace_token(payload) == "ya29.test-token"


def test_secretary_auth_config_supports_object_and_legacy_string():
    auth_config = {
        "type": "secretary_google_workspace_postgres",
        "service": "contacts",
    }

    assert auth_type_from_config(auth_config) == "secretary_google_workspace_postgres"
    assert auth_type_from_config("oauth") == "oauth"
    assert service_from_auth_config(auth_config) == "people"


def test_google_mcp_v1_mutation_risk_map():
    gmail_auth = {
        "type": "secretary_google_workspace_postgres",
        "service": "gmail",
    }
    calendar_auth = {
        "type": "secretary_google_workspace_postgres",
        "service": "calendar",
    }
    people_auth = {
        "type": "secretary_google_workspace_postgres",
        "service": "people",
    }

    assert google_mcp_tool_requires_approval(gmail_auth, "create_draft")
    assert google_mcp_tool_requires_approval(calendar_auth, "delete_event")
    assert not google_mcp_tool_requires_approval(gmail_auth, "search_threads")
    assert not google_mcp_tool_requires_approval(calendar_auth, "list_events")
    assert not google_mcp_tool_requires_approval(people_auth, "search_contacts")


def test_google_mcp_approval_envelope_carries_exact_tool_call():
    data = build_google_mcp_approval_data(
        server_name="google_gmail",
        tool_name="create_draft",
        args={"to": ["owner@example.com"], "subject": "Hello"},
        auth_config={
            "type": "secretary_google_workspace_postgres",
            "service": "gmail",
        },
    )

    assert data["command"] == "mcp:google_gmail/create_draft"
    assert data["server_name"] == "google_gmail"
    assert data["tool_name"] == "create_draft"
    assert data["arguments"] == {"to": ["owner@example.com"], "subject": "Hello"}
    assert data["ui"]["component"] == "approval_card"
    assert data["ui"]["props"]["toolName"] == "create_draft"
    assert data["ui"]["props"]["arguments"] == data["arguments"]
    assert data["ui"]["actions"][0]["payload"]["approvalId"] == data["approval_id"]
