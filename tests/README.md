# Test Suite

Three layers, one command:

```powershell
# install once
pip install -r requirements-dev.txt

# run everything
pytest

# pick a layer
pytest -m unit
pytest -m integration
pytest -m e2e
```

## Layers

| Layer | Tests | Speed | Network | What it covers |
|---|---|---|---|---|
| `unit` | 196 | <2s | none | Pure logic: rate limiter, LLM router (parsing, usage, switch), tools (intent routing, schema validity, **dispatch contract for all 73 tools**), `_friendly_error` helper, Smartsheet client (mock transport: retry, cache, CRUD), SQLite DB CRUD (users, sessions, conversations, favorites, audit, webhooks), agent helpers, **agent.run() loop** (tool dispatch, confirm approve/reject, parse-error recovery, MAX_TOOL_ROUNDS, image/chart events), MCP server smoke (52 tools registered). |
| `integration` | 41 | medium | Smartsheet API | Real Smartsheet read calls + create→modify→delete lifecycle on a throwaway sheet. **All FastAPI HTTP routes**: `/health`, `/api/env-status`, `/api/providers`, `/api/validate-token`, `/api/session`, `/api/usage`, `/api/disconnect`, `/api/switch-model`, `/api/csv-to-sheet`, `/api/conversations/*`, `/api/favorites/*`, `/api/audit`, `/api/export`, `/api/webhook-events`, `/api/smartsheet-webhook` (challenge + payload fan-out), `/api/quick-connect`, `/api/generate-title`, `/api/pin-sheet`. |
| `e2e` | 9 | medium | Smartsheet API | Full FastAPI lifespan, session bootstrap, WebSocket handshake, agent loop with stubbed LLM, suggestions extraction, **cancel mid-stream**, **destructive-tool confirm/reject over WS**, **rate-limit response**, multi-turn conversation in a single connection. |

Integration & e2e tests **automatically skip** when `SMARTSHEET_TOKEN` /
`SHEET_ID` are not present in `.env`.

## How LLM calls are handled

* Unit tests **never** call any LLM.
* Integration tests **never** call any LLM (they only hit Smartsheet).
* E2E tests stub `LLMRouter.chat_stream` so no OpenAI / Anthropic /
  OpenRouter call is made — the test is deterministic and free.

If you want to exercise a real LLM end-to-end one day, set
`ENABLE_LIVE_LLM=1` and add a test marked `@pytest.mark.live_llm`.

## Test database isolation

The `tmp_db` fixture (in `conftest.py`) points `backend.db` at a fresh
SQLite file in a per-test temp directory and resets the module's
`_initialized` flag, so the production `data/smartsheet_ctrl.sqlite` is
never touched.

E2E tests do the same in-line before importing `backend.app` so the
FastAPI lifespan boots its own clean DB.

## Coverage report

```powershell
pytest --cov=backend --cov-report=term-missing --cov-report=html
```

HTML report lands in `htmlcov/index.html`.
