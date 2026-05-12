# Google Pulse

Google Pulse is a FastAPI-based incident response webhook that classifies incoming monitoring alerts, stores incident context in PostgreSQL with pgvector, and fans out response actions through MCP-backed agents for Google Chat, Google Docs, Google Calendar, GitHub, and Cloud Logging.

The repo is structured so the webhook can run safely on a laptop with external side effects disabled, while the same code can enable real integrations in Cloud Run or a fully configured local environment.

## What It Does

- Accepts monitoring alerts over HTTP.
- Classifies incidents as `P0`, `P1`, or `P2`.
- Stores incidents and vector memory in PostgreSQL.
- Searches similar past incidents with Gemini embeddings plus pgvector.
- Dispatches Chat, Docs, and Calendar actions concurrently through MCP.
- Runs a second-phase ADK analysis agent that can query GitHub and Cloud Logging, then write findings back to the incident doc.

## Architecture

```text
simulator/fire_incident.py
          |
          v
  webhook/main.py
          |
          v
orchestrator/agent.py
  |      |       |
  |      |       +--> database/db.py
  |      |
  |      +--> orchestrator/classifier.py
  |
  +--> orchestrator/mcp_clients.py
           |
           +--> agents/chat_agent/mcp_server.py
           +--> agents/docs_agent/mcp_server.py
           +--> agents/calendar_agent/mcp_server.py
           +--> agents/github_agent/mcp_server.py
           +--> agents/logging_agent/mcp_server.py
```

## Project Layout

```text
auth/            OAuth helper for Google APIs
database/        Schema, DB access, and seed data
orchestrator/    Classification, orchestration, MCP client wiring, ADK analysis
agents/          MCP servers for Chat, Docs, Calendar, GitHub, and Logging
simulator/       Demo alert generator
ui/              Static dashboard served by FastAPI
webhook/         FastAPI entrypoint and routes
```

## Runtime Modes

By default, local development is conservative:

- `ENABLE_DATABASE=false`
- `ENABLE_EXTERNAL_ACTIONS=false`
- `ENABLE_GENAI_FEATURES=false`
- `ENABLE_LLM_CLASSIFIER=false`

That means you can boot the app without immediately needing a live database, Gemini, Google Workspace credentials, or running MCP servers.

In Cloud Run, the code enables those features automatically when `K_SERVICE` is present, unless you override them.

## Requirements

- Python 3.13+
- Docker Desktop
- PostgreSQL with `pgvector` if you want DB-backed features
- Google Cloud / Google Workspace credentials if you want live integrations

## Local Setup

1. Create the environment.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
```

2. Create local config.

```powershell
Copy-Item .env.example .env
```

3. Start PostgreSQL.

```powershell
docker compose up -d postgres
```

4. Apply the schema.

```powershell
psql -h localhost -U postgres -d crisiscommand -f database/schema.sql
```

5. Start the webhook.

```powershell
uvicorn webhook.main:app --host 127.0.0.1 --port 8000 --reload
```

6. Fire a sample alert.

```powershell
python simulator/fire_incident.py p0_payments
```

Available simulator scenarios:

- `p0_payments`
- `p1_auth`
- `p2_storage`

## Running With Real MCP Servers

For SSE-based local integration testing:

```powershell
.\start-local-mcp.ps1
```

This starts the Chat, Docs, Calendar, GitHub, and Logging MCP servers and then runs the FastAPI app with `MCP_TRANSPORT=sse`.

If you stay on the default `stdio` transport, the orchestrator starts the MCP servers as subprocesses on demand.

## Seeding Incident Memory

To preload the vector memory with sample incidents:

```powershell
python database/seed.py
```

This requires:

- a reachable PostgreSQL instance with the schema applied
- `GOOGLE_API_KEY` in `.env`

## Important Endpoints

- `POST /webhook` - main event intake
- `POST /webhook/monitoring-alert` - direct alert endpoint used by the dashboard
- `GET /health` - health check
- `GET /incidents` - recent incidents for the UI
- `POST /incidents/{incident_id}/resolution` - attach resolution notes
- `GET /` - serves `ui/dashboard.html`

## Key Environment Variables

See `.env.example` for the full list. The most important ones are:

- `GOOGLE_API_KEY`
- `DATABASE_URL`
- `SIMULATOR_SECRET`
- `CHAT_SPACE_ID`
- `DOCS_FOLDER_ID`
- `ONCALL_EMAIL`
- `GITHUB_TOKEN`
- `GITHUB_REPO`
- `GOOGLE_CLOUD_PROJECT`
- `MCP_TRANSPORT`

## Deployment

The main deployment script is:

```powershell
.\deploy-secure.ps1
```

It reads local configuration, builds the API image, pushes it, creates secrets, and deploys the webhook to Cloud Run.

## Notes

- Secrets such as `.env`, `credentials.json`, `token.json`, and `service_account.json` are intentionally ignored and should stay local.
- `uv.lock` is present, but the project is also installable with plain `pip install -e .`.
- Some Google Chat event handling exists in the webhook, but the active Chat MCP implementation currently posts text messages rather than card-based UI.
