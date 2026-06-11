# Secretary Local Hermes Proof

This proof keeps Hermes local and upstream-friendly:

- Hermes keeps using SQLite `state.db` as the canonical store.
- Postgres mirrors committed `sessions` and `messages` rows for inspection.
- Hermes runs its existing API server.
- Bedrock uses Hermes' existing native Converse provider.

## 1. Install

```bash
cd /Users/jc/cnc/hermes-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[api-server,bedrock,postgres]'
```

## 2. Local Hermes Home

Hermes needs `HERMES_HOME` before it can load `$HERMES_HOME/.env`, so use the
local wrapper instead of shell exports. The wrapper sets `HERMES_HOME` and then
loads:

```bash
/Users/jc/cnc/hermes-agent/.local/hermes-users/dev-jc/.env
```

The local file is ignored by git through `.local/`.

```bash
mkdir -p /Users/jc/cnc/hermes-agent/.local/hermes-users/dev-jc
$EDITOR /Users/jc/cnc/hermes-agent/.local/hermes-users/dev-jc/.env
```

## 3. Postgres Mirror

For Secretary local development, reuse the existing Secretary Postgres instance
and put Hermes mirror tables in a separate `hermes` schema. Do not start another
database if Secretary Postgres is already running. Put this in
`.local/hermes-users/dev-jc/.env`:

```dotenv
HERMES_POSTGRES_MIRROR_ENABLED=true
HERMES_POSTGRES_DSN=postgresql://postgres:postgres@127.0.0.1:5432/secretary
HERMES_POSTGRES_SCHEMA=hermes
HERMES_POSTGRES_HOME_KEY=dev-jc
HERMES_POSTGRES_STRICT=false
```

If you are using a live Secretary Postgres port-forward, keep the same database
credentials but point the host/port at the forwarded endpoint, for example:

```bash
HERMES_POSTGRES_DSN=postgresql://postgres:postgres@127.0.0.1:5555/secretary
```

The compose file is only a fallback for running Hermes outside the Secretary
workspace, when no Secretary Postgres is available:

```bash
docker compose -f docker-compose.postgres.yml up -d
```

For that fallback database only, use:

```dotenv
HERMES_POSTGRES_MIRROR_ENABLED=true
HERMES_POSTGRES_DSN=postgresql://hermes:hermes@127.0.0.1:55432/hermes
HERMES_POSTGRES_SCHEMA=hermes
HERMES_POSTGRES_HOME_KEY=dev-jc
HERMES_POSTGRES_STRICT=false
```

Create tables, backfill existing SQLite rows, and compare counts:

```bash
scripts/secretary-hermes-local migrate
scripts/secretary-hermes-local backfill
scripts/secretary-hermes-local status
```

## 4. Bedrock

Put this in `.local/hermes-users/dev-jc/.env`:

```dotenv
AWS_PROFILE=jc_secretary
AWS_REGION=us-east-1
HERMES_BEDROCK_REGION=us-east-1
HERMES_INFERENCE_PROVIDER=bedrock
HERMES_INFERENCE_MODEL=us.anthropic.claude-haiku-4-5-20251001-v1:0
BEDROCK_BASE_URL=https://bedrock-runtime.us-east-1.amazonaws.com
```

Then verify SSO when needed:

```bash
aws sts get-caller-identity --profile jc_secretary
scripts/secretary-hermes-local sync-config
```

Use a Bedrock Converse-compatible model enabled for the AWS account and region.
To change models, edit `HERMES_INFERENCE_MODEL` in the env file; the local
wrapper syncs it into Hermes runtime config before `gateway`, `smoke`, or `cli`
runs.

## 5. API Server

Put this in `.local/hermes-users/dev-jc/.env`:

```dotenv
API_SERVER_ENABLED=true
API_SERVER_HOST=127.0.0.1
API_SERVER_PORT=8642
API_SERVER_KEY=change-me-local-dev
API_SERVER_CORS_ORIGINS=http://localhost:3055,http://127.0.0.1:3055
```

Start the gateway:

```bash
scripts/secretary-hermes-local gateway
```

Smoke test in another shell:

```bash
scripts/secretary-hermes-local smoke
```

The smoke client reads the same `.env`, calls `/v1/capabilities`, creates a
session, sends a chat message, and reads messages back. For raw curl testing,
load `.local/hermes-users/dev-jc/.env` in that shell first.

```bash
scripts/secretary-hermes-local status
```
