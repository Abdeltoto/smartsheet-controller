import json
import os
import httpx
from backend.smartsheet_client import SmartsheetClient


def _t(name: str, desc: str, props: dict | None = None, required: list[str] | None = None) -> dict:
    params = {"type": "object", "properties": props or {}, "required": required or []}
    return {"name": name, "description": desc, "parameters": params}


_S = {"type": "string", "description": "Sheet ID"}

TOOL_DEFINITIONS = [
    _t("get_current_user", "Get current user profile."),
    _t("list_sheets", "List all sheets (name + ID)."),
    _t("search", "Search across the account.",
       {"query": {"type": "string"}}, ["query"]),
    _t("search_sheet", "Search within a sheet.",
       {"sheet_id": _S, "query": {"type": "string"}}, ["sheet_id", "query"]),
    _t("list_workspaces", "List all workspaces."),
    _t("get_workspace", "Get workspace contents.",
       {"workspace_id": {"type": "string"}}, ["workspace_id"]),
    _t("list_folders", "List home folders."),
    _t("get_folder", "Get folder contents.",
       {"folder_id": {"type": "string"}}, ["folder_id"]),
    _t("create_folder", "Create a folder.",
       {"name": {"type": "string"}, "parent_folder_id": {"type": "string"}}, ["name"]),
    _t("get_recent_items", "Get recent sheets and favorites."),

    _t("get_sheet_summary", "Get sheet structure: columns, types, row count.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("read_rows", "Read rows. Optional range like '1-10'.",
       {"sheet_id": _S, "row_range": {"type": "string"}}, ["sheet_id"]),
    _t("get_row", "Get one row by ID.",
       {"sheet_id": _S, "row_id": {"type": "integer"}}, ["sheet_id", "row_id"]),
    _t("get_cell_history", "Get cell change history.",
       {"sheet_id": _S, "row_id": {"type": "integer"}, "column_id": {"type": "integer"}},
       ["sheet_id", "row_id", "column_id"]),
    _t("get_summary_fields", "Get sheet summary fields.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("analyze_sheet", "Deep analysis: structure, data, formulas, cross-refs.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("detect_issues", "Scan for errors, empty cols, inconsistencies.",
       {"sheet_id": _S}, ["sheet_id"]),

    _t("add_rows", "Add rows. Each row: {col_name: value}.",
       {"sheet_id": _S, "rows": {"type": "array", "items": {"type": "object"}}},
       ["sheet_id", "rows"]),
    _t("update_rows", "Update cells. [{rowId, cells: {col: {value/formula}}}].",
       {"sheet_id": _S, "updates": {"type": "array", "items": {"type": "object"}}},
       ["sheet_id", "updates"]),
    _t("delete_rows", "Delete rows by IDs.",
       {"sheet_id": _S, "row_ids": {"type": "array", "items": {"type": "integer"}}},
       ["sheet_id", "row_ids"]),
    _t("move_rows", "Move rows to another sheet.",
       {"sheet_id": _S, "row_ids": {"type": "array", "items": {"type": "integer"}},
        "destination_sheet_id": {"type": "string"}},
       ["sheet_id", "row_ids", "destination_sheet_id"]),
    _t("copy_rows", "Copy rows to another sheet.",
       {"sheet_id": _S, "row_ids": {"type": "array", "items": {"type": "integer"}},
        "destination_sheet_id": {"type": "string"}},
       ["sheet_id", "row_ids", "destination_sheet_id"]),
    _t("sort_sheet", "Sort by columns. [{columnId, direction}].",
       {"sheet_id": _S, "sort_criteria": {"type": "array", "items": {"type": "object"}}},
       ["sheet_id", "sort_criteria"]),

    _t("add_column", "Add column (TEXT_NUMBER/DATE/PICKLIST/CHECKBOX/CONTACT_LIST/DATETIME/DURATION/PREDECESSOR).",
       {"sheet_id": _S, "title": {"type": "string"}, "col_type": {"type": "string"},
        "index": {"type": "integer"}, "description": {"type": "string"}},
       ["sheet_id", "title", "col_type", "index"]),
    _t("update_column", "Update column title/description.",
       {"sheet_id": _S, "column_id": {"type": "integer"},
        "title": {"type": "string"}, "description": {"type": "string"}},
       ["sheet_id", "column_id"]),
    _t("delete_column", "Delete a column.",
       {"sheet_id": _S, "column_id": {"type": "integer"}}, ["sheet_id", "column_id"]),

    _t("create_sheet", "Create sheet with columns.",
       {"name": {"type": "string"}, "columns": {"type": "array", "items": {"type": "object"}}},
       ["name", "columns"]),
    _t("delete_sheet", "Delete a sheet.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("rename_sheet", "Rename a sheet.",
       {"sheet_id": _S, "new_name": {"type": "string"}}, ["sheet_id", "new_name"]),
    _t("copy_sheet", "Copy a sheet.",
       {"sheet_id": _S, "new_name": {"type": "string"},
        "destination_id": {"type": "string"}, "destination_type": {"type": "string"}},
       ["sheet_id", "new_name"]),
    _t("move_sheet", "Move sheet to folder/workspace.",
       {"sheet_id": _S, "destination_id": {"type": "string"}, "destination_type": {"type": "string"}},
       ["sheet_id", "destination_id"]),

    _t("list_cross_sheet_refs", "List cross-sheet references.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("create_cross_sheet_ref", "Create cross-sheet ref for formulas.",
       {"sheet_id": _S, "name": {"type": "string"}, "source_sheet_id": {"type": "integer"},
        "start_column_id": {"type": "integer"}, "end_column_id": {"type": "integer"}},
       ["sheet_id", "name", "source_sheet_id", "start_column_id", "end_column_id"]),
    _t("list_automations", "List automation rules.",
       {"sheet_id": _S}, ["sheet_id"]),

    _t("list_shares", "List sharing permissions.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("share_sheet", "Share sheet with a user.",
       {"sheet_id": _S, "email": {"type": "string"}, "access_level": {"type": "string"}},
       ["sheet_id", "email"]),
    _t("update_share", "Update share permission.",
       {"sheet_id": _S, "share_id": {"type": "string"}, "access_level": {"type": "string"}},
       ["sheet_id", "share_id", "access_level"]),
    _t("delete_share", "Remove share access.",
       {"sheet_id": _S, "share_id": {"type": "string"}}, ["sheet_id", "share_id"]),

    _t("list_discussions", "List sheet discussions.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("list_row_discussions", "List row discussions.",
       {"sheet_id": _S, "row_id": {"type": "integer"}}, ["sheet_id", "row_id"]),
    _t("add_comment", "Reply to a discussion.",
       {"sheet_id": _S, "discussion_id": {"type": "integer"}, "text": {"type": "string"}},
       ["sheet_id", "discussion_id", "text"]),
    _t("create_row_discussion", "Start discussion on a row.",
       {"sheet_id": _S, "row_id": {"type": "integer"}, "text": {"type": "string"}},
       ["sheet_id", "row_id", "text"]),

    _t("list_attachments", "List sheet attachments.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("get_attachment", "Get attachment details/URL.",
       {"sheet_id": _S, "attachment_id": {"type": "integer"}}, ["sheet_id", "attachment_id"]),

    _t("list_reports", "List all reports."),
    _t("get_report", "Get report data.",
       {"report_id": {"type": "string"}}, ["report_id"]),
    _t("list_dashboards", "List all dashboards."),
    _t("get_dashboard", "Get dashboard details.",
       {"dashboard_id": {"type": "string"}}, ["dashboard_id"]),

    _t("generate_image", "Generate an image from a text description using DALL-E. Returns an image URL displayed in chat.",
       {"prompt": {"type": "string", "description": "Detailed image description"},
        "size": {"type": "string", "description": "1024x1024, 1792x1024, or 1024x1792"}},
       ["prompt"]),

    _t("list_templates", "List public templates."),
    _t("list_webhooks", "List webhooks."),
    _t("create_webhook", "Create a webhook.",
       {"name": {"type": "string"}, "sheet_id": _S, "callback_url": {"type": "string"}},
       ["name", "sheet_id", "callback_url"]),
    _t("delete_webhook", "Delete a webhook.",
       {"webhook_id": {"type": "integer"}}, ["webhook_id"]),
]


async def execute_tool(client: SmartsheetClient, tool_name: str, args: dict) -> str:
    try:
        result = await _dispatch(client, tool_name, args)
        return json.dumps(result, default=str, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


async def _dispatch(client: SmartsheetClient, name: str, args: dict):
    if name == "get_current_user":
        return await client.get_current_user()
    if name == "list_sheets":
        return await client.list_sheets()
    if name == "search":
        return await client.search_everything(args["query"])
    if name == "search_sheet":
        return await client.search_sheet(args["sheet_id"], args["query"])
    if name == "list_workspaces":
        return await client.list_workspaces()
    if name == "get_workspace":
        return await client.get_workspace(args["workspace_id"])
    if name == "list_folders":
        return await client.list_home_folders()
    if name == "get_folder":
        return await client.get_folder(args["folder_id"])
    if name == "create_folder":
        return await client.create_folder(args["name"], args.get("parent_folder_id"))
    if name == "get_recent_items":
        return await client.get_recent_items()
    if name == "get_sheet_summary":
        return await client.get_sheet_summary(args["sheet_id"])
    if name == "read_rows":
        return await client.get_rows(args["sheet_id"], args.get("row_range"))
    if name == "get_row":
        return await client.get_row(args["sheet_id"], args["row_id"])
    if name == "get_cell_history":
        return await client.get_cell_history(args["sheet_id"], args["row_id"], args["column_id"])
    if name == "get_summary_fields":
        return await client.get_sheet_summary_fields(args["sheet_id"])
    if name == "analyze_sheet":
        return await client.analyze_sheet(args["sheet_id"])
    if name == "detect_issues":
        return await client.detect_issues(args["sheet_id"])
    if name == "add_rows":
        return await client.add_rows(args["sheet_id"], args["rows"])
    if name == "update_rows":
        return await client.update_rows(args["sheet_id"], args["updates"])
    if name == "delete_rows":
        return await client.delete_rows(args["sheet_id"], args["row_ids"])
    if name == "move_rows":
        return await client.move_rows(args["sheet_id"], args["row_ids"], args["destination_sheet_id"])
    if name == "copy_rows":
        return await client.copy_rows(args["sheet_id"], args["row_ids"], args["destination_sheet_id"])
    if name == "sort_sheet":
        return await client.sort_sheet(args["sheet_id"], args["sort_criteria"])
    if name == "add_column":
        return await client.add_column(
            args["sheet_id"], args["title"], args["col_type"],
            args["index"], args.get("description", ""),
        )
    if name == "update_column":
        kwargs = {}
        if "title" in args:
            kwargs["title"] = args["title"]
        if "description" in args:
            kwargs["description"] = args["description"]
        return await client.update_column(args["sheet_id"], args["column_id"], **kwargs)
    if name == "delete_column":
        return await client.delete_column(args["sheet_id"], args["column_id"])
    if name == "create_sheet":
        return await client.create_sheet(args["name"], args["columns"])
    if name == "delete_sheet":
        return await client.delete_sheet(args["sheet_id"])
    if name == "rename_sheet":
        return await client.rename_sheet(args["sheet_id"], args["new_name"])
    if name == "copy_sheet":
        return await client.copy_sheet(
            args["sheet_id"], args["new_name"],
            args.get("destination_id"), args.get("destination_type", "home"),
        )
    if name == "move_sheet":
        return await client.move_sheet(
            args["sheet_id"], args["destination_id"],
            args.get("destination_type", "folder"),
        )
    if name == "list_cross_sheet_refs":
        return await client.list_cross_sheet_refs(args["sheet_id"])
    if name == "create_cross_sheet_ref":
        return await client.create_cross_sheet_ref(
            args["sheet_id"], args["name"],
            args["source_sheet_id"], args["start_column_id"], args["end_column_id"],
        )
    if name == "list_automations":
        return await client.list_automations(args["sheet_id"])
    if name == "list_shares":
        return await client.list_shares(args["sheet_id"])
    if name == "share_sheet":
        return await client.share_sheet(args["sheet_id"], args["email"], args.get("access_level", "VIEWER"))
    if name == "update_share":
        return await client.update_share(args["sheet_id"], args["share_id"], args["access_level"])
    if name == "delete_share":
        return await client.delete_share(args["sheet_id"], args["share_id"])
    if name == "list_discussions":
        return await client.list_discussions(args["sheet_id"])
    if name == "list_row_discussions":
        return await client.list_row_discussions(args["sheet_id"], args["row_id"])
    if name == "add_comment":
        return await client.add_comment(args["sheet_id"], args["discussion_id"], args["text"])
    if name == "create_row_discussion":
        return await client.create_discussion_on_row(args["sheet_id"], args["row_id"], args["text"])
    if name == "list_attachments":
        return await client.list_attachments(args["sheet_id"])
    if name == "get_attachment":
        return await client.get_attachment(args["sheet_id"], args["attachment_id"])
    if name == "list_reports":
        return await client.list_reports()
    if name == "get_report":
        return await client.get_report(args["report_id"])
    if name == "list_dashboards":
        return await client.list_dashboards()
    if name == "get_dashboard":
        return await client.get_dashboard(args["dashboard_id"])
    if name == "generate_image":
        return await _generate_image(args.get("prompt", ""), args.get("size", "1024x1024"))
    if name == "list_templates":
        return await client.list_public_templates()
    if name == "list_webhooks":
        return await client.list_webhooks()
    if name == "create_webhook":
        return await client.create_webhook(args["name"], args["sheet_id"], args["callback_url"])
    if name == "delete_webhook":
        return await client.delete_webhook(args["webhook_id"])
    return {"error": f"Unknown tool: {name}"}


async def _generate_image(prompt: str, size: str = "1024x1024") -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {"error": "OPENAI_API_KEY not configured — cannot generate images."}

    valid_sizes = {"1024x1024", "1792x1024", "1024x1792"}
    if size not in valid_sizes:
        size = "1024x1024"

    async with httpx.AsyncClient(timeout=120) as http:
        resp = await http.post(
            "https://api.openai.com/v1/images/generations",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": "dall-e-3", "prompt": prompt, "n": 1, "size": size, "quality": "standard"},
        )
        data = resp.json()

    if resp.status_code != 200:
        return {"error": data.get("error", {}).get("message", f"API error {resp.status_code}")}

    image_url = data["data"][0]["url"]
    revised_prompt = data["data"][0].get("revised_prompt", prompt)
    return {"image_url": image_url, "revised_prompt": revised_prompt, "__is_image__": True}
