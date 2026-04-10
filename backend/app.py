import os
import json
import traceback
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

import logging

from backend.smartsheet_client import SmartsheetClient
from backend.llm_router import LLMRouter
from backend.agent import Agent

log = logging.getLogger(__name__)

load_dotenv(override=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield

app = FastAPI(title="Smartsheet Controller", lifespan=lifespan)


class SessionConfig(BaseModel):
    smartsheet_token: str
    sheet_id: str
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o"
    llm_api_key: str = ""


sessions: dict[str, dict] = {}


@app.get("/api/env-status")
async def env_status():
    ss_token = os.getenv("SMARTSHEET_TOKEN", "").strip()
    sheet_id = os.getenv("SHEET_ID", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    provider = "openai" if openai_key else ("anthropic" if anthropic_key else "")

    return {
        "ready": bool(ss_token and sheet_id and (openai_key or anthropic_key)),
        "has_smartsheet_token": bool(ss_token),
        "has_sheet_id": bool(sheet_id),
        "sheet_id": sheet_id,
        "provider": provider,
        "has_llm_key": bool(openai_key or anthropic_key),
    }


async def _build_sheet_context(ss_client: SmartsheetClient, sheet_id: str) -> dict:
    summary = await ss_client.get_sheet_summary(sheet_id)

    sample_rows = []
    try:
        sheet_data = await ss_client.get_sheet(sheet_id, page_size=5)
        columns_by_id = {c["id"]: c["title"] for c in sheet_data.get("columns", [])}
        for row in sheet_data.get("rows", [])[:5]:
            row_dict = {}
            for cell in row.get("cells", []):
                col_name = columns_by_id.get(cell.get("columnId"), "?")
                val = cell.get("displayValue") or cell.get("value", "")
                if val is not None and str(val).strip():
                    row_dict[col_name] = str(val)
            if row_dict:
                sample_rows.append(row_dict)
    except Exception as e:
        log.warning(f"Could not fetch sample rows: {e}")

    all_sheets = []
    try:
        all_sheets = await ss_client.list_sheets()
    except Exception as e:
        log.warning(f"Could not list sheets: {e}")

    return {
        "summary": summary,
        "sample_rows": sample_rows,
        "all_sheets": all_sheets,
    }


def _build_welcome(summary: dict) -> dict:
    name = summary.get("name", "Unknown")
    cols = summary.get("columnCount", 0)
    rows = summary.get("totalRowCount", 0)
    col_names = [c["title"] for c in summary.get("columns", [])[:8]]
    col_str = ", ".join(f"`{c}`" for c in col_names)
    more = f" +{len(summary.get('columns', [])) - 8} autres" if len(summary.get("columns", [])) > 8 else ""

    content = (
        f"### Connecte a **{name}**\n\n"
        f"| Info | Valeur |\n|---|---|\n"
        f"| Lignes | **{rows}** |\n"
        f"| Colonnes | **{cols}** |\n"
        f"| Structure | {col_str}{more} |\n\n"
        f"Je suis votre expert Smartsheet. Que souhaitez-vous faire ?"
    )

    suggestions = [
        "Montre la structure de ma sheet",
        "Analyse les problemes",
        "Lis les 20 premieres lignes",
        "Qui a acces a cette sheet ?",
    ]

    return {"type": "response", "content": content, "suggestions": suggestions}


async def _create_session(ss_client: SmartsheetClient, sheet_id: str, llm: LLMRouter) -> dict:
    ctx = await _build_sheet_context(ss_client, sheet_id)
    summary = ctx["summary"]

    session_id = f"s-{len(sessions)}"
    sessions[session_id] = {
        "smartsheet": ss_client,
        "llm": llm,
        "agent": Agent(llm, ss_client, sheet_id, ctx),
        "messages": [],
        "sheet_id": sheet_id,
        "sheet_name": summary["name"],
        "context": ctx,
    }

    all_sheets = [{"id": s.get("id"), "name": s.get("name")} for s in ctx.get("all_sheets", [])]

    return {
        "session_id": session_id,
        "sheet": summary,
        "all_sheets": all_sheets,
        "welcome": _build_welcome(summary),
    }


@app.post("/api/session")
async def create_session(config: SessionConfig):
    model = config.llm_model.strip()
    if not model:
        model = "gpt-4o-mini" if config.llm_provider == "openai" else "claude-sonnet-4-20250514"

    api_key = config.llm_api_key or os.getenv(
        "OPENAI_API_KEY" if config.llm_provider == "openai" else "ANTHROPIC_API_KEY", ""
    )
    if not api_key:
        return JSONResponse({"error": f"No API key for {config.llm_provider}. Provide one in the form or set it in .env"}, status_code=400)

    ss_client = SmartsheetClient(config.smartsheet_token)
    try:
        result = await _create_session(ss_client, config.sheet_id, LLMRouter(config.llm_provider, model, api_key))
    except Exception as e:
        await ss_client.close()
        return JSONResponse({"error": f"Cannot access sheet: {e}"}, status_code=400)

    return result


@app.post("/api/quick-connect")
async def quick_connect():
    ss_token = os.getenv("SMARTSHEET_TOKEN", "").strip()
    sheet_id = os.getenv("SHEET_ID", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if not ss_token:
        return JSONResponse({"error": "SMARTSHEET_TOKEN not set in .env"}, status_code=400)
    if not sheet_id:
        return JSONResponse({"error": "SHEET_ID not set in .env"}, status_code=400)

    if openai_key:
        provider, model, api_key = "openai", "gpt-4o-mini", openai_key
    elif anthropic_key:
        provider, model, api_key = "anthropic", "claude-sonnet-4-20250514", anthropic_key
    else:
        return JSONResponse({"error": "No LLM API key in .env (OPENAI_API_KEY or ANTHROPIC_API_KEY)"}, status_code=400)

    ss_client = SmartsheetClient(ss_token)
    try:
        result = await _create_session(ss_client, sheet_id, LLMRouter(provider, model, api_key))
    except Exception as e:
        await ss_client.close()
        return JSONResponse({"error": f"Cannot access sheet: {e}"}, status_code=400)

    return result


class SwitchSheetRequest(BaseModel):
    session_id: str
    sheet_id: str


@app.post("/api/switch-sheet")
async def switch_sheet(req: SwitchSheetRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    ss_client: SmartsheetClient = session["smartsheet"]
    try:
        ctx = await _build_sheet_context(ss_client, req.sheet_id)
    except Exception as e:
        return JSONResponse({"error": f"Cannot access sheet: {e}"}, status_code=400)

    summary = ctx["summary"]
    session["agent"] = Agent(session["llm"], ss_client, req.sheet_id, ctx)
    session["messages"] = []
    session["sheet_id"] = req.sheet_id
    session["sheet_name"] = summary["name"]
    session["context"] = ctx

    return {"sheet": summary, "welcome": _build_welcome(summary)}


@app.websocket("/ws/{session_id}")
async def websocket_chat(ws: WebSocket, session_id: str):
    await ws.accept()
    session = sessions.get(session_id)
    if not session:
        await ws.send_json({"type": "error", "content": "Invalid session"})
        await ws.close()
        return

    agent: Agent = session["agent"]
    messages: list = session["messages"]

    try:
        while True:
            data = await ws.receive_text()
            payload = json.loads(data)
            user_msg = payload.get("message", "")

            messages.append({"role": "user", "content": user_msg})

            async def send_event(event):
                await ws.send_json(event)

            try:
                await agent.run(messages, on_event=send_event)
            except Exception as e:
                error_msg = str(e)
                if "quota" in error_msg.lower() or "rate" in error_msg.lower():
                    error_msg = f"LLM API error: {error_msg}\n\nPlease check your API key quota and billing."
                elif "model" in error_msg.lower():
                    error_msg = f"Model error: {error_msg}\n\nPlease verify the model name is correct."
                else:
                    error_msg = f"Error: {error_msg}"
                print(f"Agent error: {traceback.format_exc()}")
                await ws.send_json({"type": "response", "content": error_msg})

    except WebSocketDisconnect:
        pass


app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
async def index():
    return FileResponse("frontend/index.html")
