"""
Smartsheet MCP Server — Full-control MCP interface for Smartsheet accounts.

Exposes reading, writing, analysis, sharing, automation, and account management
as MCP tools. Run standalone via stdio or mount on an ASGI server.

Usage:
    # stdio (for Claude Desktop / Cursor / any MCP client)
    SMARTSHEET_TOKEN=xxx python -m backend.mcp_server

    # streamable-http (for browser-based clients)
    SMARTSHEET_TOKEN=xxx python -m backend.mcp_server --transport http
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.session import ServerSession

from backend.smartsheet_client import SmartsheetClient

load_dotenv()


# ═══════════════════════════════════════════════════════════════
# Lifespan — manages the SmartsheetClient across the server's life
# ═══════════════════════════════════════════════════════════════

@dataclass
class AppContext:
    client: SmartsheetClient


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    token = os.getenv("SMARTSHEET_TOKEN", "")
    if not token:
        raise RuntimeError("SMARTSHEET_TOKEN environment variable is required")
    client = SmartsheetClient(token)
    try:
        yield AppContext(client=client)
    finally:
        await client.close()


# ═══════════════════════════════════════════════════════════════
# Server
# ═══════════════════════════════════════════════════════════════

mcp = FastMCP(
    "Smartsheet Controller",
    instructions=(
        "Full-control MCP server for Smartsheet. "
        "Provides tools for reading, writing, analyzing sheets, "
        "managing columns/rows, sharing, comments, attachments, "
        "automations, cross-sheet references, workspaces, folders, "
        "reports, dashboards, and more."
    ),
    lifespan=app_lifespan,
)


def _get_client(ctx: Context[ServerSession, AppContext]) -> SmartsheetClient:
    return ctx.request_context.lifespan_context.client


def _json(data: Any) -> str:
    return json.dumps(data, default=str, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
# ACCOUNT & NAVIGATION
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_get_current_user(ctx: Context[ServerSession, AppContext]) -> str:
    """Get the current authenticated user's profile: name, email, locale, timezone, and account info."""
    client = _get_client(ctx)
    return _json(await client.get_current_user())


@mcp.tool()
async def smartsheet_list_sheets(ctx: Context[ServerSession, AppContext]) -> str:
    """List all sheets accessible with the current Smartsheet token. Returns sheet IDs and names."""
    client = _get_client(ctx)
    return _json(await client.list_sheets())


@mcp.tool()
async def smartsheet_search(
    query: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Search across the entire Smartsheet account for sheets, rows, discussions, and attachments matching the query."""
    client = _get_client(ctx)
    return _json(await client.search_everything(query))


@mcp.tool()
async def smartsheet_search_sheet(
    sheet_id: str,
    query: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Search within a specific sheet for rows matching the query string."""
    client = _get_client(ctx)
    return _json(await client.search_sheet(sheet_id, query))


@mcp.tool()
async def smartsheet_list_workspaces(ctx: Context[ServerSession, AppContext]) -> str:
    """List all workspaces in the account with their IDs and names."""
    client = _get_client(ctx)
    return _json(await client.list_workspaces())


@mcp.tool()
async def smartsheet_get_workspace(
    workspace_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Get details of a workspace including its sheets, folders, reports, and dashboards."""
    client = _get_client(ctx)
    return _json(await client.get_workspace(workspace_id))


@mcp.tool()
async def smartsheet_list_folders(ctx: Context[ServerSession, AppContext]) -> str:
    """List all top-level folders in the user's home."""
    client = _get_client(ctx)
    return _json(await client.list_home_folders())


@mcp.tool()
async def smartsheet_get_folder(
    folder_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Get the contents of a folder: sheets, sub-folders, reports, dashboards."""
    client = _get_client(ctx)
    return _json(await client.get_folder(folder_id))


@mcp.tool()
async def smartsheet_create_folder(
    name: str,
    ctx: Context[ServerSession, AppContext],
    parent_folder_id: str = "",
) -> str:
    """Create a new folder. If parent_folder_id is given, creates a sub-folder; otherwise creates at home level."""
    client = _get_client(ctx)
    parent = parent_folder_id if parent_folder_id else None
    return _json(await client.create_folder(name, parent))


@mcp.tool()
async def smartsheet_get_recent_items(ctx: Context[ServerSession, AppContext]) -> str:
    """Get the user's home view: recent sheets, favorites, workspaces, and folders."""
    client = _get_client(ctx)
    return _json(await client.get_recent_items())


# ═══════════════════════════════════════════════════════════════
# SHEET READING & UNDERSTANDING
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_get_sheet_summary(
    sheet_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Get a sheet's structure: name, total row count, and all column definitions (title, type, ID, index). Use this first to understand a sheet before reading data."""
    client = _get_client(ctx)
    return _json(await client.get_sheet_summary(sheet_id))


@mcp.tool()
async def smartsheet_read_rows(
    sheet_id: str,
    ctx: Context[ServerSession, AppContext],
    row_range: str = "",
) -> str:
    """Read row data from a sheet. Returns cell values, display values, and formulas. Optionally filter by row range (e.g. '1-10', '36-71'). Omit row_range to read all rows."""
    client = _get_client(ctx)
    rng = row_range if row_range else None
    return _json(await client.get_rows(sheet_id, rng))


@mcp.tool()
async def smartsheet_get_row(
    sheet_id: str,
    row_id: int,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Get a single row by its row ID, including all cell values and column info."""
    client = _get_client(ctx)
    return _json(await client.get_row(sheet_id, row_id))


@mcp.tool()
async def smartsheet_get_cell_history(
    sheet_id: str,
    row_id: int,
    column_id: int,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Get the modification history of a specific cell. Shows who changed it, when, and previous values."""
    client = _get_client(ctx)
    return _json(await client.get_cell_history(sheet_id, row_id, column_id))


@mcp.tool()
async def smartsheet_get_summary_fields(
    sheet_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Get the sheet summary fields (the summary section at the top of a sheet with key metadata)."""
    client = _get_client(ctx)
    return _json(await client.get_sheet_summary_fields(sheet_id))


@mcp.tool()
async def smartsheet_analyze_sheet(
    sheet_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Deep analysis of a sheet: complete structure, all data with formulas, detected sections, cross-sheet references, automations, picklist values, and column classifications (manual vs formula vs cross-sheet)."""
    client = _get_client(ctx)
    await ctx.info(f"Analyzing sheet {sheet_id}...")
    result = await client.analyze_sheet(sheet_id)
    await ctx.info(f"Analysis complete: {result.get('totalRows', 0)} rows, {len(result.get('columns', []))} columns")
    return _json(result)


@mcp.tool()
async def smartsheet_detect_issues(
    sheet_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Scan a sheet for common issues: formula errors, empty columns, missing descriptions, inconsistent picklist values, and other data quality problems."""
    client = _get_client(ctx)
    return _json(await client.detect_issues(sheet_id))


# ═══════════════════════════════════════════════════════════════
# SHEET WRITING & MODIFICATION
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_add_rows(
    sheet_id: str,
    rows: list[dict[str, Any]],
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Add new rows to the bottom of a sheet. Each row is a dict mapping column names to values. Use {"formula": "=..."} for formula cells."""
    client = _get_client(ctx)
    return _json(await client.add_rows(sheet_id, rows))


@mcp.tool()
async def smartsheet_update_rows(
    sheet_id: str,
    updates: list[dict[str, Any]],
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Update cells in existing rows. Each update has rowId and cells: {"ColumnName": {"value": ...}} or {"ColumnName": {"formula": "=..."}}."""
    client = _get_client(ctx)
    return _json(await client.update_rows(sheet_id, updates))


@mcp.tool()
async def smartsheet_delete_rows(
    sheet_id: str,
    row_ids: list[int],
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Delete rows by their row IDs. This is destructive and cannot be undone."""
    client = _get_client(ctx)
    return _json(await client.delete_rows(sheet_id, row_ids))


@mcp.tool()
async def smartsheet_move_rows(
    sheet_id: str,
    row_ids: list[int],
    destination_sheet_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Move rows from one sheet to another. Rows are removed from the source sheet."""
    client = _get_client(ctx)
    return _json(await client.move_rows(sheet_id, row_ids, destination_sheet_id))


@mcp.tool()
async def smartsheet_copy_rows(
    sheet_id: str,
    row_ids: list[int],
    destination_sheet_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Copy rows from one sheet to another. Original rows remain in the source sheet."""
    client = _get_client(ctx)
    return _json(await client.copy_rows(sheet_id, row_ids, destination_sheet_id))


@mcp.tool()
async def smartsheet_sort_sheet(
    sheet_id: str,
    sort_criteria: list[dict[str, Any]],
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Sort a sheet by one or more columns. Each criterion: {"columnId": <id>, "direction": "ASCENDING"|"DESCENDING"}."""
    client = _get_client(ctx)
    return _json(await client.sort_sheet(sheet_id, sort_criteria))


# ═══════════════════════════════════════════════════════════════
# COLUMN MANAGEMENT
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_add_column(
    sheet_id: str,
    title: str,
    col_type: str,
    index: int,
    ctx: Context[ServerSession, AppContext],
    description: str = "",
) -> str:
    """Add a new column to a sheet. Types: TEXT_NUMBER, DATE, PICKLIST, CHECKBOX, CONTACT_LIST, DATETIME, DURATION, PREDECESSOR, ABSTRACT_DATETIME."""
    client = _get_client(ctx)
    return _json(await client.add_column(sheet_id, title, col_type, index, description))


@mcp.tool()
async def smartsheet_update_column(
    sheet_id: str,
    column_id: int,
    ctx: Context[ServerSession, AppContext],
    title: str = "",
    description: str = "",
) -> str:
    """Update a column's title and/or description."""
    client = _get_client(ctx)
    kwargs: dict[str, str] = {}
    if title:
        kwargs["title"] = title
    if description:
        kwargs["description"] = description
    if not kwargs:
        raise ToolError("Provide at least title or description to update")
    return _json(await client.update_column(sheet_id, column_id, **kwargs))


@mcp.tool()
async def smartsheet_delete_column(
    sheet_id: str,
    column_id: int,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Delete a column from a sheet. This removes all data in that column permanently."""
    client = _get_client(ctx)
    return _json(await client.delete_column(sheet_id, column_id))


# ═══════════════════════════════════════════════════════════════
# SHEET MANAGEMENT
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_create_sheet(
    name: str,
    columns: list[dict[str, Any]],
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Create a new sheet at the top level. Columns: [{"title": "Name", "type": "TEXT_NUMBER", "primary": true}, ...]."""
    client = _get_client(ctx)
    return _json(await client.create_sheet(name, columns))


@mcp.tool()
async def smartsheet_delete_sheet(
    sheet_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Delete an entire sheet permanently. Cannot be undone."""
    client = _get_client(ctx)
    return _json(await client.delete_sheet(sheet_id))


@mcp.tool()
async def smartsheet_rename_sheet(
    sheet_id: str,
    new_name: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Rename a sheet."""
    client = _get_client(ctx)
    return _json(await client.rename_sheet(sheet_id, new_name))


@mcp.tool()
async def smartsheet_copy_sheet(
    sheet_id: str,
    new_name: str,
    ctx: Context[ServerSession, AppContext],
    destination_id: str = "",
    destination_type: str = "home",
) -> str:
    """Copy a sheet. Optionally specify a destination folder or workspace ID."""
    client = _get_client(ctx)
    dest = destination_id if destination_id else None
    return _json(await client.copy_sheet(sheet_id, new_name, dest, destination_type))


@mcp.tool()
async def smartsheet_move_sheet(
    sheet_id: str,
    destination_id: str,
    ctx: Context[ServerSession, AppContext],
    destination_type: str = "folder",
) -> str:
    """Move a sheet to a different folder or workspace."""
    client = _get_client(ctx)
    return _json(await client.move_sheet(sheet_id, destination_id, destination_type))


# ═══════════════════════════════════════════════════════════════
# CROSS-SHEET REFERENCES
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_list_cross_sheet_refs(
    sheet_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """List all cross-sheet references defined on a sheet."""
    client = _get_client(ctx)
    return _json(await client.list_cross_sheet_refs(sheet_id))


@mcp.tool()
async def smartsheet_create_cross_sheet_ref(
    sheet_id: str,
    name: str,
    source_sheet_id: int,
    start_column_id: int,
    end_column_id: int,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Create a cross-sheet reference. Used in formulas as {name} to pull data from another sheet."""
    client = _get_client(ctx)
    return _json(await client.create_cross_sheet_ref(sheet_id, name, source_sheet_id, start_column_id, end_column_id))


# ═══════════════════════════════════════════════════════════════
# AUTOMATIONS
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_list_automations(
    sheet_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """List all automation rules on a sheet with their names, enabled status, and action types."""
    client = _get_client(ctx)
    return _json(await client.list_automations(sheet_id))


# ═══════════════════════════════════════════════════════════════
# SHARING & COLLABORATION
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_list_shares(
    sheet_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """List all sharing permissions on a sheet: who has access and their permission level."""
    client = _get_client(ctx)
    return _json(await client.list_shares(sheet_id))


@mcp.tool()
async def smartsheet_share_sheet(
    sheet_id: str,
    email: str,
    ctx: Context[ServerSession, AppContext],
    access_level: str = "VIEWER",
) -> str:
    """Share a sheet with a user by email. Access levels: VIEWER, EDITOR, EDITOR_SHARE, ADMIN, OWNER."""
    client = _get_client(ctx)
    return _json(await client.share_sheet(sheet_id, email, access_level))


@mcp.tool()
async def smartsheet_update_share(
    sheet_id: str,
    share_id: str,
    access_level: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Update a sharing permission level. Access levels: VIEWER, EDITOR, EDITOR_SHARE, ADMIN."""
    client = _get_client(ctx)
    return _json(await client.update_share(sheet_id, share_id, access_level))


@mcp.tool()
async def smartsheet_delete_share(
    sheet_id: str,
    share_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Remove sharing access from a sheet for a specific share."""
    client = _get_client(ctx)
    return _json(await client.delete_share(sheet_id, share_id))


# ═══════════════════════════════════════════════════════════════
# DISCUSSIONS & COMMENTS
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_list_discussions(
    sheet_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """List all discussions on a sheet, including their comments."""
    client = _get_client(ctx)
    return _json(await client.list_discussions(sheet_id))


@mcp.tool()
async def smartsheet_list_row_discussions(
    sheet_id: str,
    row_id: int,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """List discussions attached to a specific row."""
    client = _get_client(ctx)
    return _json(await client.list_row_discussions(sheet_id, row_id))


@mcp.tool()
async def smartsheet_add_comment(
    sheet_id: str,
    discussion_id: int,
    text: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Add a reply comment to an existing discussion thread."""
    client = _get_client(ctx)
    return _json(await client.add_comment(sheet_id, discussion_id, text))


@mcp.tool()
async def smartsheet_create_row_discussion(
    sheet_id: str,
    row_id: int,
    text: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Start a new discussion thread on a specific row with an initial comment."""
    client = _get_client(ctx)
    return _json(await client.create_discussion_on_row(sheet_id, row_id, text))


# ═══════════════════════════════════════════════════════════════
# ATTACHMENTS
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_list_attachments(
    sheet_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """List all attachments on a sheet with their names, types, sizes, and download URLs."""
    client = _get_client(ctx)
    return _json(await client.list_attachments(sheet_id))


@mcp.tool()
async def smartsheet_get_attachment(
    sheet_id: str,
    attachment_id: int,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Get details of a specific attachment including its download URL."""
    client = _get_client(ctx)
    return _json(await client.get_attachment(sheet_id, attachment_id))


# ═══════════════════════════════════════════════════════════════
# REPORTS & DASHBOARDS
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_list_reports(ctx: Context[ServerSession, AppContext]) -> str:
    """List all reports accessible to the current user."""
    client = _get_client(ctx)
    return _json(await client.list_reports())


@mcp.tool()
async def smartsheet_get_report(
    report_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Get the data from a report, including columns and rows from source sheets."""
    client = _get_client(ctx)
    return _json(await client.get_report(report_id))


@mcp.tool()
async def smartsheet_list_dashboards(ctx: Context[ServerSession, AppContext]) -> str:
    """List all dashboards (sights) accessible to the current user."""
    client = _get_client(ctx)
    return _json(await client.list_dashboards())


@mcp.tool()
async def smartsheet_get_dashboard(
    dashboard_id: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Get details of a dashboard including its widgets and their configurations."""
    client = _get_client(ctx)
    return _json(await client.get_dashboard(dashboard_id))


# ═══════════════════════════════════════════════════════════════
# TEMPLATES
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_list_templates(ctx: Context[ServerSession, AppContext]) -> str:
    """List all public Smartsheet templates available for creating new sheets."""
    client = _get_client(ctx)
    return _json(await client.list_public_templates())


# ═══════════════════════════════════════════════════════════════
# WEBHOOKS
# ═══════════════════════════════════════════════════════════════

@mcp.tool()
async def smartsheet_list_webhooks(ctx: Context[ServerSession, AppContext]) -> str:
    """List all webhooks configured on the account."""
    client = _get_client(ctx)
    return _json(await client.list_webhooks())


@mcp.tool()
async def smartsheet_create_webhook(
    name: str,
    sheet_id: str,
    callback_url: str,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Create a webhook to receive notifications when a sheet changes. Requires a publicly accessible callback URL."""
    client = _get_client(ctx)
    return _json(await client.create_webhook(name, sheet_id, callback_url))


@mcp.tool()
async def smartsheet_delete_webhook(
    webhook_id: int,
    ctx: Context[ServerSession, AppContext],
) -> str:
    """Delete a webhook by its ID."""
    client = _get_client(ctx)
    return _json(await client.delete_webhook(webhook_id))


# ═══════════════════════════════════════════════════════════════
# RESOURCES — read-only data exposed to MCP clients
# ═══════════════════════════════════════════════════════════════

@mcp.resource("smartsheet://sheets")
async def resource_sheets() -> str:
    """List of all accessible Smartsheet sheets."""
    token = os.getenv("SMARTSHEET_TOKEN", "")
    if not token:
        return '{"error": "SMARTSHEET_TOKEN not set"}'
    client = SmartsheetClient(token)
    try:
        sheets = await client.list_sheets()
        return _json(sheets)
    finally:
        await client.close()


@mcp.resource("smartsheet://user")
async def resource_user() -> str:
    """Current authenticated Smartsheet user profile."""
    token = os.getenv("SMARTSHEET_TOKEN", "")
    if not token:
        return '{"error": "SMARTSHEET_TOKEN not set"}'
    client = SmartsheetClient(token)
    try:
        user = await client.get_current_user()
        return _json(user)
    finally:
        await client.close()


@mcp.resource("smartsheet://sheets/{sheet_id}/summary")
async def resource_sheet_summary(sheet_id: str) -> str:
    """Summary and structure of a specific sheet."""
    token = os.getenv("SMARTSHEET_TOKEN", "")
    if not token:
        return '{"error": "SMARTSHEET_TOKEN not set"}'
    client = SmartsheetClient(token)
    try:
        summary = await client.get_sheet_summary(sheet_id)
        return _json(summary)
    finally:
        await client.close()


# ═══════════════════════════════════════════════════════════════
# PROMPTS — reusable conversation starters for LLMs
# ═══════════════════════════════════════════════════════════════

@mcp.prompt(title="Analyze Sheet")
def prompt_analyze(sheet_id: str) -> str:
    """Prompt to perform a comprehensive analysis of a Smartsheet."""
    return (
        f"Please perform a deep analysis of Smartsheet {sheet_id}. "
        "First use smartsheet_analyze_sheet to understand its structure, data flow, "
        "and relationships. Then use smartsheet_detect_issues to find problems. "
        "Finally, provide a structured report with:\n"
        "1. Sheet purpose and overview\n"
        "2. Data flow (manual vs formula vs cross-sheet columns)\n"
        "3. Issues found with severity\n"
        "4. Improvement suggestions categorized as Quick Wins / Medium / Strategic"
    )


@mcp.prompt(title="Sheet Audit")
def prompt_audit(sheet_id: str) -> str:
    """Prompt to audit sharing, automations, and structure of a sheet."""
    return (
        f"Please audit Smartsheet {sheet_id}. Check:\n"
        "1. Who has access (use smartsheet_list_shares)\n"
        "2. What automations exist (use smartsheet_list_automations)\n"
        "3. Cross-sheet references (use smartsheet_list_cross_sheet_refs)\n"
        "4. Data quality issues (use smartsheet_detect_issues)\n"
        "5. Attachment inventory (use smartsheet_list_attachments)\n"
        "Provide a security and quality audit report."
    )


@mcp.prompt(title="Explore Account")
def prompt_explore() -> str:
    """Prompt to explore the full Smartsheet account."""
    return (
        "Please explore my Smartsheet account. Use these tools in order:\n"
        "1. smartsheet_get_current_user — identify who I am\n"
        "2. smartsheet_list_sheets — list all my sheets\n"
        "3. smartsheet_list_workspaces — list workspaces\n"
        "4. smartsheet_list_folders — list folders\n"
        "5. smartsheet_list_reports — list reports\n"
        "6. smartsheet_list_dashboards — list dashboards\n"
        "Provide a complete overview of my account structure."
    )


# ═══════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════

def main():
    transport = "stdio"
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            t = sys.argv[idx + 1].lower()
            if t in ("http", "streamable-http"):
                transport = "streamable-http"
            elif t == "sse":
                transport = "sse"

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
