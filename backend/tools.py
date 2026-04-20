import json
import os
import re
import httpx
from backend.smartsheet_client import SmartsheetClient


def _t(name: str, desc: str, props: dict | None = None, required: list[str] | None = None) -> dict:
    params = {"type": "object", "properties": props or {}, "required": required or []}
    return {"name": name, "description": desc, "parameters": params}


_S = {"type": "string", "description": "Sheet ID"}

TOOL_DEFINITIONS = [
    _t("get_current_user", "Get the authenticated user's profile (name, email, locale)."),
    _t("list_sheets", "List every sheet the user can access (name + ID). Use when the user asks 'quelles sheets j'ai', 'mes feuilles', or needs to pick a different sheet."),
    _t("search", "Account-wide full-text search across sheet names, cells, comments, attachments. Use for 'find', 'cherche', 'where is X'.",
       {"query": {"type": "string"}}, ["query"]),
    _t("search_sheet", "Search inside ONE specific sheet's cells (faster than full-account search). Use when the user asks 'find X dans cette feuille'.",
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

    _t("get_sheet_summary", "Get sheet schema: name, totalRowCount, columnCount, and the columns array (id/title/type/index). Use this BEFORE any write to confirm exact column titles. Cheap — call once per turn, do not loop.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("read_rows", "Read actual row data (cells with values). row_range is '1-10' or '5-5'. max_rows defaults to 500. Returns a list of rows with their `id`, `rowNumber`, and cells. Use this to discover real Smartsheet rowIds — do NOT invent integers like 1, 2, 3.",
       {"sheet_id": _S, "row_range": {"type": "string"}, "max_rows": {"type": "integer", "description": "Max rows to load (default 500, max 5000)"}}, ["sheet_id"]),
    _t("get_row", "Get one full row by its real Smartsheet rowId (large integer from get_sheet_summary or read_rows). Useful as a verification read after `update_rows`.",
       {"sheet_id": _S, "row_id": {"type": "integer"}}, ["sheet_id", "row_id"]),
    _t("get_cell_history", "Get audit trail of edits for one specific cell (rowId + columnId required). Use only when the user asks 'who changed X' or 'when was X modified'.",
       {"sheet_id": _S, "row_id": {"type": "integer"}, "column_id": {"type": "integer"}},
       ["sheet_id", "row_id", "column_id"]),
    _t("get_summary_fields", "Get the sheet-level summary fields (KPIs at the top of the sheet). Different from `get_sheet_summary` (which returns columns).",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("analyze_sheet", "Deep analytical pass: structure, data quality, formula usage, cross-sheet refs. Use when the user asks 'analyse cette feuille', 'audit', 'overview'. Cheaper than chaining 5 reads.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("detect_issues", "Scan for problems: error cells (#REF, #INVALID), empty columns, missing descriptions, inconsistencies. Returns prioritized issues (low/medium/high).",
       {"sheet_id": _S}, ["sheet_id"]),

    _t("add_rows", "Add new ROWS (horizontal records) to an existing sheet. Each item in `rows` is an object {ColumnName: value, OtherColumn: value} where keys are EXISTING column titles (case-sensitive). For formulas use {ColumnName: {\"formula\": \"=...\"}}. ⚠ DO NOT use this to create a new column — that's `add_column`. ⚠ Column titles MUST already exist on the sheet (check with get_sheet_summary first); unknown columns are rejected by the schema-guard with the list of valid columns.",
       {"sheet_id": _S, "rows": {"type": "array", "items": {"type": "object"}}},
       ["sheet_id", "rows"]),
    _t("update_rows", "Modify cells in EXISTING rows. `updates` is a list of {rowId: <real Smartsheet id>, cells: {ColumnName: {value: ...} OR {formula: \"=...\"}}}. Get rowIds from `read_rows` or `get_sheet_summary` — never invent them. ⚠ Argument key is `updates`, NOT `rows`. ⚠ Column titles must exist (schema-guard enforces).",
       {"sheet_id": _S, "updates": {"type": "array", "items": {"type": "object"}}},
       ["sheet_id", "updates"]),
    _t("delete_rows", "Permanently delete rows by their real Smartsheet rowIds (large integers, not row numbers like 1/2/3). Destructive — requires user confirmation.",
       {"sheet_id": _S, "row_ids": {"type": "array", "items": {"type": "integer"}}},
       ["sheet_id", "row_ids"]),
    _t("move_rows", "Move rows from this sheet to another sheet (rows leave the source). Destructive.",
       {"sheet_id": _S, "row_ids": {"type": "array", "items": {"type": "integer"}},
        "destination_sheet_id": {"type": "string"}},
       ["sheet_id", "row_ids", "destination_sheet_id"]),
    _t("copy_rows", "Copy rows from this sheet to another sheet (rows stay in source). Destructive on destination.",
       {"sheet_id": _S, "row_ids": {"type": "array", "items": {"type": "integer"}},
        "destination_sheet_id": {"type": "string"}},
       ["sheet_id", "row_ids", "destination_sheet_id"]),
    _t("sort_sheet", "Sort rows in place. `sort_criteria` is a list of {columnId: <int>, direction: 'ASCENDING'|'DESCENDING'}. Use columnIds from get_sheet_summary, not titles.",
       {"sheet_id": _S, "sort_criteria": {"type": "array", "items": {"type": "object"}}},
       ["sheet_id", "sort_criteria"]),

    _t("add_column", "Add a new COLUMN (vertical field) to a sheet. Use when the user asks 'add a column', 'create a column', 'new column', 'ajoute une colonne', 'rajoute une colonne', 'nouvelle colonne', 'crée une colonne'. col_type ∈ {TEXT_NUMBER, DATE, DATETIME, PICKLIST, CHECKBOX, CONTACT_LIST, DURATION, PREDECESSOR}. `index` = position (0 = first, columnCount = append at end). ⚠ NOT for adding rows — that's `add_rows`. Pick CHECKBOX for boolean data, DATE for dates, TEXT_NUMBER for everything else if unsure.",
       {"sheet_id": _S, "title": {"type": "string"}, "col_type": {"type": "string"},
        "index": {"type": "integer"}, "description": {"type": "string"}},
       ["sheet_id", "title", "col_type", "index"]),
    _t("update_column", "Rename a column or change its description. column_id from get_sheet_summary. Use when user says 'renomme la colonne X en Y'. Cannot change col_type.",
       {"sheet_id": _S, "column_id": {"type": "integer"},
        "title": {"type": "string"}, "description": {"type": "string"}},
       ["sheet_id", "column_id"]),
    _t("delete_column", "Permanently delete a column AND all its data. Destructive — requires user confirmation. Cannot delete the primary column.",
       {"sheet_id": _S, "column_id": {"type": "integer"}}, ["sheet_id", "column_id"]),

    _t("create_sheet", "Create a brand-new sheet with a name and an initial set of columns. `columns` is a list of {title, type, primary?: bool} — exactly ONE column must have primary=true. Use when user says 'crée une feuille', 'new sheet'.",
       {"name": {"type": "string"}, "columns": {"type": "array", "items": {"type": "object"}}},
       ["name", "columns"]),
    _t("delete_sheet", "Permanently delete an entire sheet (and all rows, attachments, history). Destructive — confirm first.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("rename_sheet", "Rename a sheet (only changes the title; sheet_id stays the same).",
       {"sheet_id": _S, "new_name": {"type": "string"}}, ["sheet_id", "new_name"]),
    _t("copy_sheet", "Copy a sheet.",
       {"sheet_id": _S, "new_name": {"type": "string"},
        "destination_id": {"type": "string"}, "destination_type": {"type": "string"}},
       ["sheet_id", "new_name"]),
    _t("move_sheet", "Move sheet to folder/workspace.",
       {"sheet_id": _S, "destination_id": {"type": "string"}, "destination_type": {"type": "string"}},
       ["sheet_id", "destination_id"]),

    _t("list_cross_sheet_refs",
       "List existing cross-sheet references on this sheet (named ranges that "
       "formulas can use via {RefName}). Call this BEFORE create_cross_sheet_ref "
       "to avoid duplicates and to discover already-available refs.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("create_cross_sheet_ref",
       "MANDATORY FIRST STEP for any formula that pulls data from ANOTHER sheet "
       "(VLOOKUP, INDEX/MATCH, INDEX(COLLECT(...)), SUMIFS / COUNTIFS / "
       "AVERAGEIF, etc.). Creates a named range on `sheet_id` (the sheet where "
       "the formula will live) that points to one or more columns of "
       "`source_sheet_id` (the sheet you want to read from). The formula will "
       "then reference it as `{name}` (single braces). REQUIRED workflow: "
       "(1) get the source sheet ID via list_sheets / search; "
       "(2) call get_sheet_summary on the source to obtain column IDs; "
       "(3) call create_cross_sheet_ref ONCE per column you need (single column "
       "→ start_column_id == end_column_id; whole row range needed by VLOOKUP "
       "→ start = first col id, end = last col id); "
       "(4) write the formula via add_rows / update_rows / add_column with "
       "`=INDEX(COLLECT({name}, ...))` or `=SUMIFS({name}, ...)`. NEVER write a "
       "formula that uses `{xyz}` without having created `xyz` first — that "
       "produces #INVALID REF.",
       {"sheet_id": _S,
        "name": {"type": "string"},
        "source_sheet_id": {"type": "integer"},
        "start_column_id": {"type": "integer"},
        "end_column_id": {"type": "integer"}},
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
    _t("list_row_discussions", "List discussions/comments on ONE specific row. row_id MUST be a real Smartsheet rowId from get_sheet_summary or read_rows. ⚠ Do NOT iterate this on fabricated IDs (1, 2, 3) — that produces 404s and noise.",
       {"sheet_id": _S, "row_id": {"type": "integer"}}, ["sheet_id", "row_id"]),
    _t("add_comment", "Add a comment INSIDE an existing discussion (reply). For a brand-new discussion on a row, use `create_row_discussion` instead.",
       {"sheet_id": _S, "discussion_id": {"type": "integer"}, "text": {"type": "string"}},
       ["sheet_id", "discussion_id", "text"]),
    _t("create_row_discussion", "Start discussion on a row.",
       {"sheet_id": _S, "row_id": {"type": "integer"}, "text": {"type": "string"}},
       ["sheet_id", "row_id", "text"]),

    _t("list_attachments", "List sheet attachments.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("list_row_attachments", "List attachments on ONE specific row. row_id MUST be a real Smartsheet rowId. ⚠ Do NOT loop this over fabricated IDs (1, 2, 3) — for sheet-wide attachments use `list_attachments` instead.",
       {"sheet_id": _S, "row_id": {"type": "integer"}}, ["sheet_id", "row_id"]),
    _t("get_attachment", "Get attachment details/URL.",
       {"sheet_id": _S, "attachment_id": {"type": "integer"}}, ["sheet_id", "attachment_id"]),
    _t("attach_url_to_sheet", "Attach a URL/link (Google Drive, Dropbox, OneDrive, web link) to the sheet.",
       {"sheet_id": _S, "name": {"type": "string"}, "url": {"type": "string"},
        "attachment_type": {"type": "string", "description": "LINK | GOOGLE_DRIVE | DROPBOX | BOX_COM | EVERNOTE | EGNYTE | ONEDRIVE (default LINK)"},
        "description": {"type": "string"}},
       ["sheet_id", "name", "url"]),
    _t("attach_url_to_row", "Attach a URL/link to a specific row.",
       {"sheet_id": _S, "row_id": {"type": "integer"}, "name": {"type": "string"},
        "url": {"type": "string"}, "attachment_type": {"type": "string"},
        "description": {"type": "string"}},
       ["sheet_id", "row_id", "name", "url"]),
    _t("delete_attachment", "Delete a sheet attachment by ID.",
       {"sheet_id": _S, "attachment_id": {"type": "integer"}}, ["sheet_id", "attachment_id"]),

    _t("list_sheet_forms", "List forms on a sheet (Smartsheet API exposure is limited; falls back to permalink).",
       {"sheet_id": _S}, ["sheet_id"]),

    _t("get_automation", "Get details of one automation rule.",
       {"sheet_id": _S, "rule_id": {"type": "integer"}}, ["sheet_id", "rule_id"]),
    _t("update_automation", "Enable/disable or rename an automation rule. Smartsheet does NOT support creating rules via API.",
       {"sheet_id": _S, "rule_id": {"type": "integer"},
        "enabled": {"type": "boolean"}, "name": {"type": "string"},
        "action": {"type": "object", "description": "Optional new action object (advanced)"}},
       ["sheet_id", "rule_id"]),
    _t("delete_automation", "Delete an automation rule.",
       {"sheet_id": _S, "rule_id": {"type": "integer"}}, ["sheet_id", "rule_id"]),

    _t("list_row_proofs", "List proofs on a row (Premium feature; returns availability).",
       {"sheet_id": _S, "row_id": {"type": "integer"}}, ["sheet_id", "row_id"]),
    _t("create_row_proof_from_url", "Create a proof from a URL on a row (Premium).",
       {"sheet_id": _S, "row_id": {"type": "integer"}, "name": {"type": "string"},
        "url": {"type": "string"}, "version_name": {"type": "string"}},
       ["sheet_id", "row_id", "name", "url"]),

    _t("list_update_requests", "List pending update requests on a sheet.",
       {"sheet_id": _S}, ["sheet_id"]),
    _t("create_update_request", "Send an update request to one or more emails for specific row(s).",
       {"sheet_id": _S,
        "send_to_emails": {"type": "array", "items": {"type": "string"}},
        "row_ids": {"type": "array", "items": {"type": "integer"}},
        "column_ids": {"type": "array", "items": {"type": "integer"}, "description": "Optional: restrict to these columns"},
        "subject": {"type": "string"}, "message": {"type": "string"},
        "cc_me": {"type": "boolean"}, "include_attachments": {"type": "boolean"},
        "include_discussions": {"type": "boolean"}},
       ["sheet_id", "send_to_emails", "row_ids"]),
    _t("delete_update_request", "Cancel an outstanding update request.",
       {"sheet_id": _S, "update_request_id": {"type": "integer"}},
       ["sheet_id", "update_request_id"]),

    _t("list_workspace_shares", "List sharing permissions on a workspace.",
       {"workspace_id": {"type": "string"}}, ["workspace_id"]),
    _t("share_workspace", "Share a workspace with a user (cascades to all sheets in it).",
       {"workspace_id": {"type": "string"}, "email": {"type": "string"},
        "access_level": {"type": "string", "description": "VIEWER | EDITOR | EDITOR_SHARE | ADMIN | OWNER"}},
       ["workspace_id", "email"]),
    _t("update_workspace_share", "Change a user's access level on a workspace.",
       {"workspace_id": {"type": "string"}, "share_id": {"type": "string"},
        "access_level": {"type": "string"}},
       ["workspace_id", "share_id", "access_level"]),
    _t("delete_workspace_share", "Remove a user from a workspace.",
       {"workspace_id": {"type": "string"}, "share_id": {"type": "string"}},
       ["workspace_id", "share_id"]),

    _t("create_cell_link", "Create a one-way live cell link: target cell receives data from a source cell. DIFFERENT from cross-sheet references.",
       {"target_sheet_id": _S, "target_row_id": {"type": "integer"}, "target_column_id": {"type": "integer"},
        "source_sheet_id": {"type": "integer"}, "source_row_id": {"type": "integer"}, "source_column_id": {"type": "integer"}},
       ["target_sheet_id", "target_row_id", "target_column_id",
        "source_sheet_id", "source_row_id", "source_column_id"]),

    _t("update_webhook", "Enable/disable or reconfigure a webhook in place (avoids delete + recreate).",
       {"webhook_id": {"type": "integer"}, "enabled": {"type": "boolean"},
        "name": {"type": "string"},
        "events": {"type": "array", "items": {"type": "string"}},
        "callback_url": {"type": "string"}},
       ["webhook_id"]),

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

    _t("generate_chart", "Generate an inline chart from data. Returns a Chart.js spec rendered in chat.",
       {"chart_type": {"type": "string", "description": "bar, line, pie, doughnut, radar, polarArea"},
        "title": {"type": "string", "description": "Chart title"},
        "labels": {"type": "array", "items": {"type": "string"}, "description": "X-axis labels or segment labels"},
        "datasets": {"type": "array", "items": {"type": "object"}, "description": "Array of {label, data: number[], backgroundColor?, borderColor?}"}},
       ["chart_type", "labels", "datasets"]),

    _t("list_templates", "List public templates."),
    _t("list_webhooks", "List webhooks."),
    _t("create_webhook", "Create a webhook.",
       {"name": {"type": "string"}, "sheet_id": _S, "callback_url": {"type": "string"}},
       ["name", "sheet_id", "callback_url"]),
    _t("delete_webhook", "Delete a webhook.",
       {"webhook_id": {"type": "integer"}}, ["webhook_id"]),
]


# ─────── Intent → Tool subset (S4: token reduction) ───────
# Always-include tools (cheap reads, available regardless of intent)
_CORE_TOOLS = {
    "get_current_user", "list_sheets", "get_sheet_summary", "read_rows",
    "get_row", "search", "search_sheet",
}

_TOOLS_BY_INTENT = {
    "read": {
        "analyze_sheet", "detect_issues", "get_summary_fields", "get_cell_history",
        "list_workspaces", "get_workspace", "list_folders", "get_folder",
        "get_recent_items", "list_cross_sheet_refs", "list_automations",
        "get_automation", "list_shares", "list_discussions", "list_row_discussions",
        "list_attachments", "list_row_attachments", "get_attachment",
        "list_reports", "get_report",
        "list_dashboards", "get_dashboard", "list_templates", "list_webhooks",
        "list_sheet_forms", "list_row_proofs", "list_update_requests",
        "list_workspace_shares",
    },
    "write_row": {
        "add_rows", "update_rows", "delete_rows", "move_rows", "copy_rows",
        "sort_sheet", "create_row_discussion", "add_comment",
        "create_cell_link",
    },
    "write_structure": {
        "add_column", "update_column", "delete_column", "create_sheet",
        "delete_sheet", "rename_sheet", "copy_sheet", "move_sheet",
        "create_folder", "create_cross_sheet_ref",
    },
    "share": {
        "list_shares", "share_sheet", "update_share", "delete_share",
        "list_workspace_shares", "share_workspace", "update_workspace_share",
        "delete_workspace_share",
    },
    "attachment": {
        "list_attachments", "list_row_attachments", "get_attachment",
        "attach_url_to_sheet", "attach_url_to_row", "delete_attachment",
    },
    "automation": {
        "list_automations", "get_automation", "update_automation", "delete_automation",
    },
    "proof": {
        "list_row_proofs", "create_row_proof_from_url",
    },
    "update_request": {
        "list_update_requests", "create_update_request", "delete_update_request",
    },
    "form": {"list_sheet_forms"},
    "image": {"generate_image"},
    "chart": {"generate_chart", "analyze_sheet"},
    "webhook": {"list_webhooks", "create_webhook", "delete_webhook", "update_webhook"},
}

# Keywords that gate each intent (lowercase substrings, FR + EN).
# Substring matching is brittle for inflected languages (e.g. "ajoute une colonne"
# does not contain "ajoute colonne"), so we also use _WRITE_VERB_TOKENS below
# as a safety net.
_INTENT_KEYWORDS = {
    "write_row": [
        "add", "ajout", "rajout", "create row", "créer ligne", "creer ligne",
        "insert", "insér", "inser",
        "update", "modif", "change", "changer", "set ", "fix ", "corrige",
        "delete row", "supprime", "remov", "efface", "drop row",
        "move row", "déplace", "deplace", "copy row", "duplique",
        "sort", "trier", "tri ",
        "ligne", "row ", "rows",
        "fill", "remplis", "remplit", "remplir",
    ],
    "write_structure": [
        "column", "colonne", "colonnes",
        "add column", "ajout colonne", "ajoute colonne", "create column",
        "nouvelle colonne", "nouvelles colonnes", "ajoute une colonne",
        "ajouter une colonne", "ajouter colonne", "rajoute une colonne",
        "rajouter une colonne", "crée une colonne", "creer une colonne",
        "create a column", "make a column", "new column",
        "delete column", "supprime colonne", "supprimer la colonne",
        "supprimer une colonne", "remove column", "drop column", "vire la colonne",
        "rename column", "renomme la colonne", "renommer la colonne",
        "rename", "renomme", "renommer",
        "create sheet", "crée feuille", "nouvelle feuille", "new sheet",
        "create a sheet", "make a sheet", "créer une feuille", "creer une feuille",
        "delete sheet", "supprime feuille", "supprimer feuille",
        "copy sheet", "duplique feuille", "move sheet",
        "create folder", "nouveau dossier", "créer un dossier",
        # Cross-sheet formulas / lookups across sheets — these phrases unlock
        # `create_cross_sheet_ref` so the agent can actually build the named
        # reference required by every cross-sheet formula.
        "cross-sheet", "cross sheet", "crosssheet", "crosssheets",
        "référence", "reference", "named range", "ref to ", "named ref",
        "lookup", "vlookup", "hlookup", "xlookup", "index match",
        "ramen", "ramène", "ramener", "ramenes", "ramènes", "ramenez",
        "récup", "récupère", "récupérer", "récupères", "récupérez",
        "recup", "recupere", "recuperer", "recuperez",
        "import", "importe", "importer", "importez",
        "tire", "tirer", "tires", "tirez", "tirée", "tirées",
        "pull from", "pull data", "pull value", "pull values",
        "another sheet", "other sheet", "from another", "from other",
        "autre sheet", "autre feuille", "autres feuilles",
        "across sheet", "across sheets", "entre feuille", "entre feuilles",
        "between sheet", "between sheets", "inter-sheet", "inter sheet",
        "join sheet", "rejoindre la feuille", "rejoindre une feuille",
        "external sheet",
    ],
    "share": [
        "share", "partage", "permission", "access", "accès", "invite",
        "collaborator", "collaborateur", "workspace shar", "partage workspace",
    ],
    "attachment": [
        "attach", "pièce jointe", "piece jointe", "fichier", "file ", "upload",
        "drive", "dropbox", "onedrive", "lien vers", "link to ",
    ],
    "automation": [
        "automation", "automatisation", "workflow", "rule ", "règle",
        "trigger", "déclencheur", "auto-notif",
    ],
    "proof": [
        "proof", "épreuve", "review workflow", "approval workflow",
    ],
    "update_request": [
        "update request", "demande de mise à jour", "demande de mise a jour",
        "ask ", "demander à", "demander a",
    ],
    "form": [
        "form ", "formulaire", "form url", "lien formulaire",
    ],
    "image": [
        "image", "picture", "photo", "illustration", "draw", "dessine",
        "generate image", "génère image", "logo", "icon",
    ],
    "chart": [
        "chart", "graph", "graphique", "diagram", "plot", "visualis",
        "bar chart", "pie chart", "line chart", "trend",
    ],
    "webhook": [
        "webhook", "callback", "notification url", "subscribe",
    ],
}


# Pure write-action verbs (FR + EN). If ANY token below appears as a standalone
# word in the user message, we widen the tool subset to include BOTH write_row
# AND write_structure intents — because users frequently say "ajoute une
# colonne" / "create a row" without using the exact phrase the substring
# matcher above expects. This catches the inflection problem at the root.
_WRITE_VERB_TOKENS = {
    # English
    "add", "create", "make", "insert", "new", "build", "set", "update",
    "modify", "change", "edit", "rename", "move", "copy", "duplicate",
    "remove", "delete", "drop", "clear", "fix", "fill", "import", "upload",
    "publish", "rename",
    # French (covers most inflections)
    "ajout", "ajoute", "ajouter", "ajoutes", "ajoutez", "ajoutera", "ajouterons",
    "rajout", "rajoute", "rajouter", "rajoutes", "rajoutez",
    "crée", "créer", "crees", "créez", "creer", "cree", "créée",
    "fais", "faire", "faites", "fait",
    "modif", "modifie", "modifier", "changer", "modifiez", "changez",
    "supprim", "supprime", "supprimer", "supprimez",
    "efface", "effacer", "effacez",
    "vire", "vires", "virer", "virez", "casse", "cassez",
    "renomm", "renomme", "renommer", "renommez",
    "remplir", "remplis", "remplit", "remplissez",
    "duplique", "dupliquer", "dupliquez",
    "déplac", "déplace", "déplacer", "déplacez", "deplace", "deplacer",
    "copier", "copie", "copies", "copiez",
    "mets", "mettre", "met", "mettez",
    "passe", "passer", "passez",
    "transforme", "transformer", "transformez",
    "remplace", "remplacer", "remplacez",
    # Cross-sheet "pull data from another sheet" verbs (unlock write_structure
    # so create_cross_sheet_ref enters the toolset).
    "ramen", "ramène", "ramener", "ramenes", "ramènes",
    "récup", "récupère", "récupérer", "récupères",
    "recup", "recupere", "recuperer",
    "import", "importe", "importer", "importes", "importez",
    "tire", "tirer", "tires", "tirez",
    "pull", "pulls", "lookup", "vlookup", "hlookup", "xlookup",
    "fetch", "fetches", "bring", "brings", "join", "joins",
}

_WORD_TOKEN_RE = re.compile(r"[a-zàâäéèêëïîôöùûüç]+", re.IGNORECASE)


def _tokenize_words(text: str) -> set[str]:
    return {m.group(0).lower() for m in _WORD_TOKEN_RE.finditer(text)}


def select_tools_for_message(user_message: str, all_tools: list[dict] | None = None) -> list[dict]:
    """Return the subset of TOOL_DEFINITIONS relevant to the user's intent.
    Reduces tokens by ~70% on read-only queries while never hiding write tools
    when the user clearly asks for them.

    Layered detection:
      1. Substring keyword match per intent (precise but brittle).
      2. Verb-token safety net: any pure write verb in the message unlocks
         BOTH write_row and write_structure (catches inflection misses).
      3. Bottom safety floor: if subset is too small, return everything.
    """
    if all_tools is None:
        all_tools = TOOL_DEFINITIONS
    if not user_message:
        return all_tools  # be safe — no message means new conversation

    msg = user_message.lower()
    tokens = _tokenize_words(user_message)
    intents: set[str] = {"read"}  # always include analysis tools

    for intent, keywords in _INTENT_KEYWORDS.items():
        if any(kw in msg for kw in keywords):
            intents.add(intent)

    # Verb-token safety net — write_row + write_structure travel together
    # because they are the most-confused pair (add row vs add column).
    if tokens & _WRITE_VERB_TOKENS:
        intents.add("write_row")
        intents.add("write_structure")

    allowed: set[str] = set(_CORE_TOOLS)
    for intent in intents:
        allowed.update(_TOOLS_BY_INTENT.get(intent, set()))

    selected = [t for t in all_tools if t["name"] in allowed]
    # Safety net: if we end up with nothing useful, return everything
    if len(selected) < 8:
        return all_tools
    return selected


# Keys that may appear inside an add_rows row that are NOT column names —
# they are positional/structural hints accepted by the Smartsheet API.
_ADD_ROW_RESERVED_KEYS = {
    "cells", "toBottom", "toTop", "parentId", "siblingId", "above", "below",
    "expanded", "locked", "format",
}


def _extract_referenced_columns(payload: list[dict] | None, kind: str) -> set[str]:
    """Pull out every column-name reference from an add_rows / update_rows payload.

    kind: 'add_rows' or 'update_rows'.

    Recognises BOTH shapes the LLM produces:
      1. friendly shape:  {"ColumnName": value, ...}
      2. API shape:       {"cells": [{"columnName": "X", "value": ...}, ...]}
    Cells that use `columnId` (numeric) are skipped — IDs are already
    pre-validated by Smartsheet itself.
    """
    refs: set[str] = set()
    if not payload:
        return refs
    for row in payload:
        if not isinstance(row, dict):
            continue
        # Always inspect API-style `cells` lists, regardless of kind.
        cells = row.get("cells")
        if isinstance(cells, list):
            for cell in cells:
                if not isinstance(cell, dict):
                    continue
                # Only `columnName` is a name-reference we can validate.
                # `columnId` is a numeric handle — leave it to the API.
                col_name = cell.get("columnName")
                if isinstance(col_name, str) and col_name.strip():
                    refs.add(col_name)
        elif kind == "update_rows" and isinstance(cells, dict):
            # Friendly update_rows shape: cells={"ColName": value, ...}
            refs.update(str(k) for k in cells.keys())

        if kind == "add_rows":
            # Friendly add_rows shape: row keys are column names.
            for k in row.keys():
                if k in _ADD_ROW_RESERVED_KEYS:
                    continue
                refs.add(str(k))
    return refs


async def _validate_columns_for_write(
    client: SmartsheetClient,
    sheet_id: str,
    payload: list[dict] | None,
    kind: str,
) -> dict | None:
    """Return None if the payload only references existing columns on the sheet,
    otherwise return a structured error dict explaining exactly what's wrong.

    This is the schema-guard that prevents the silent "row created but empty"
    bug — when the LLM uses a column name that doesn't exist, the upstream
    client used to drop the cell silently. We catch it here loudly with a
    helpful message including the list of valid columns.
    """
    referenced = _extract_referenced_columns(payload, kind)
    if not referenced:
        # No column-name keys at all. Either the payload is API-shaped already
        # (cells: [{columnId, value}]) or it's empty — let the client handle it.
        return None

    try:
        sheet = await client.get_sheet(sheet_id, page_size=0)
    except Exception:
        # Couldn't pre-fetch (network blip, mock client, etc.) — fall through
        # and let the actual API call surface its own error.
        return None

    if not isinstance(sheet, dict):
        return None
    columns = sheet.get("columns") or []
    if not columns:
        return None  # empty / mock — skip validation

    valid_names = {c["title"] for c in columns if isinstance(c, dict) and "title" in c}
    if not valid_names:
        return None

    unknown = sorted(referenced - valid_names)
    if not unknown:
        return None

    tool_name = "add_rows" if kind == "add_rows" else "update_rows"
    return {
        "error": "UNKNOWN_COLUMNS",
        "tool": tool_name,
        "unknown_columns": unknown,
        "valid_columns": sorted(valid_names),
        "hint": (
            f"The column(s) {unknown} do NOT exist on sheet '{sheet.get('name', sheet_id)}'. "
            "Smartsheet column names are CASE-SENSITIVE and must match exactly. "
            "Choose ONE: "
            "(a) Use one of the valid column names listed in 'valid_columns'. "
            "(b) If you intend to CREATE a new column, call the 'add_column' tool first, "
            "then retry add_rows / update_rows referencing it. "
            "(c) Re-read the sheet schema with 'get_sheet_summary' to confirm the exact spelling. "
            "Do NOT silently retry with the same column name — it will fail again."
        ),
    }


async def execute_tool(client: SmartsheetClient, tool_name: str, args: dict) -> str:
    try:
        result = await _dispatch(client, tool_name, args)
        return json.dumps(result, default=str, ensure_ascii=False)
    except KeyError as e:
        # The LLM omitted a required argument — surface it clearly so the next
        # tool round can self-correct instead of repeating the mistake.
        missing = str(e).strip("'\"")
        hint = ""
        if tool_name == "add_rows" and missing == "rows":
            hint = " To create a new COLUMN (not a row), use the 'add_column' tool instead."
        elif tool_name == "update_rows" and missing == "updates":
            hint = " The 'update_rows' tool needs an 'updates' array, not 'rows'."
        return json.dumps({
            "error": f"Missing required argument '{missing}' for tool '{tool_name}'.{hint}",
            "missing_argument": missing,
            "tool": tool_name,
        })
    except httpx.HTTPStatusError as e:
        return json.dumps(_friendly_http_error(e, tool_name, args))
    except Exception as e:
        return json.dumps({"error": str(e), "tool": tool_name, "hint": _generic_hint(str(e), tool_name)})


def _generic_hint(err_msg: str, tool_name: str) -> str:
    """Best-effort extraction of an actionable hint for non-HTTP exceptions."""
    low = err_msg.lower()
    if "timeout" in low or "timed out" in low:
        return "Network timeout — retry once. If it persists, the Smartsheet API may be slow."
    if "ssl" in low or "certificate" in low:
        return "Secure connection issue — check the network. Do not retry blindly."
    if "json" in low and ("decode" in low or "parse" in low):
        return "Response was not valid JSON — likely a transient API hiccup. Retry once."
    return ""


def _friendly_http_error(e: httpx.HTTPStatusError, tool_name: str, args: dict) -> dict:
    """Map common Smartsheet HTTP errors to actionable hints the LLM can act on."""
    status = e.response.status_code if e.response is not None else 0
    body_preview = ""
    try:
        body_preview = e.response.text[:400] if e.response is not None else ""
    except Exception:
        body_preview = ""
    body_low = body_preview.lower()

    base = {
        "error": f"HTTP {status} from Smartsheet API",
        "tool": tool_name,
        "status_code": status,
        "response_preview": body_preview,
    }

    if status == 401:
        base["hint"] = (
            "The Smartsheet token is invalid or expired. Stop calling tools and tell the user "
            "their token needs to be refreshed."
        )
        return base
    if status == 403:
        base["hint"] = (
            "Permission denied for this operation. The user lacks the required access level "
            "(viewer/editor/admin/owner) on this sheet/workspace. Tell the user — do NOT retry."
        )
        return base
    if status == 404:
        # Distinguish row-level vs sheet-level
        if "row" in tool_name or "row_id" in args:
            base["hint"] = (
                f"Row not found. The row_id ({args.get('row_id')}) does not exist on this sheet. "
                "Smartsheet rowIds are large integers — call `read_rows` or `get_sheet_summary` first to get real IDs. "
                "Do NOT iterate this call on fabricated IDs (1, 2, 3, ...)."
            )
        elif "attachment" in tool_name:
            base["hint"] = "Attachment not found. The attachment_id may be stale or the attachment was deleted."
        elif "sheet" in tool_name:
            base["hint"] = (
                "Sheet not found. Either the sheet_id is wrong, the sheet was deleted, or it was just created "
                "and the API is eventually consistent — wait 1s and retry once."
            )
        else:
            base["hint"] = "Resource not found. Verify the ID with a list/get call before retrying."
        return base
    if status == 409:
        base["hint"] = "Conflict — the resource is in a state that doesn't allow this operation (e.g. already exists, locked)."
        return base
    if status == 429:
        base["hint"] = "Rate-limited by Smartsheet. Stop calling tools and tell the user to wait ~30s."
        return base
    if status == 400:
        if "invalid" in body_low and "column" in body_low:
            base["hint"] = (
                "Invalid column value. Most likely you tried to put a value of one type into a column of another type "
                "(e.g. boolean into TEXT_NUMBER, or date string into TEXT_NUMBER). "
                "Check the column type with `get_sheet_summary` and either pick the right column or `add_column` of the right type."
            )
        elif "formula" in body_low or "unparseable" in body_low:
            base["hint"] = (
                "Smartsheet rejected the formula. Common causes: function does not exist in Smartsheet "
                "(e.g. POWER → use `^`, CONCATENATE → use `+`, IFS → use nested IF), or `TRUE()` instead of bare `TRUE`, "
                "or wrong reference syntax. Re-read the formula catalog in your system prompt."
            )
        elif "primary" in body_low:
            base["hint"] = "create_sheet requires EXACTLY ONE column with `primary: true`."
        else:
            base["hint"] = "Bad request — check argument shapes against the tool's schema."
        return base
    if status >= 500:
        base["hint"] = "Smartsheet server error — retry the call ONCE. If it fails again, tell the user."
        return base

    base["hint"] = "Unexpected HTTP status — inspect response_preview and decide whether to retry."
    return base


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
        return await client.get_rows(args["sheet_id"], args.get("row_range"), args.get("max_rows", 500))
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
        guard = await _validate_columns_for_write(
            client, args["sheet_id"], args.get("rows"), kind="add_rows"
        )
        if guard is not None:
            return guard
        return await client.add_rows(args["sheet_id"], args["rows"])
    if name == "update_rows":
        guard = await _validate_columns_for_write(
            client, args["sheet_id"], args.get("updates"), kind="update_rows"
        )
        if guard is not None:
            return guard
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
    if name == "list_row_attachments":
        return await client.list_row_attachments(args["sheet_id"], args["row_id"])
    if name == "get_attachment":
        return await client.get_attachment(args["sheet_id"], args["attachment_id"])
    if name == "attach_url_to_sheet":
        return await client.attach_url_to_sheet(
            args["sheet_id"], args["name"], args["url"],
            args.get("attachment_type", "LINK"), args.get("description", ""),
        )
    if name == "attach_url_to_row":
        return await client.attach_url_to_row(
            args["sheet_id"], args["row_id"], args["name"], args["url"],
            args.get("attachment_type", "LINK"), args.get("description", ""),
        )
    if name == "delete_attachment":
        return await client.delete_attachment(args["sheet_id"], args["attachment_id"])
    if name == "list_sheet_forms":
        return await client.list_sheet_forms(args["sheet_id"])
    if name == "get_automation":
        return await client.get_automation(args["sheet_id"], args["rule_id"])
    if name == "update_automation":
        return await client.update_automation(
            args["sheet_id"], args["rule_id"],
            enabled=args.get("enabled"), name=args.get("name"), action=args.get("action"),
        )
    if name == "delete_automation":
        return await client.delete_automation(args["sheet_id"], args["rule_id"])
    if name == "list_row_proofs":
        return await client.list_row_proofs(args["sheet_id"], args["row_id"])
    if name == "create_row_proof_from_url":
        return await client.create_row_proof_from_url(
            args["sheet_id"], args["row_id"], args["name"], args["url"],
            args.get("version_name", "v1"),
        )
    if name == "list_update_requests":
        return await client.list_update_requests(args["sheet_id"])
    if name == "create_update_request":
        return await client.create_update_request(
            args["sheet_id"],
            send_to_emails=args["send_to_emails"],
            row_ids=args["row_ids"],
            column_ids=args.get("column_ids"),
            subject=args.get("subject"),
            message=args.get("message"),
            cc_me=args.get("cc_me", False),
            include_attachments=args.get("include_attachments", False),
            include_discussions=args.get("include_discussions", False),
        )
    if name == "delete_update_request":
        return await client.delete_update_request(args["sheet_id"], args["update_request_id"])
    if name == "list_workspace_shares":
        return await client.list_workspace_shares(args["workspace_id"])
    if name == "share_workspace":
        return await client.share_workspace(
            args["workspace_id"], args["email"], args.get("access_level", "VIEWER"),
        )
    if name == "update_workspace_share":
        return await client.update_workspace_share(
            args["workspace_id"], args["share_id"], args["access_level"],
        )
    if name == "delete_workspace_share":
        return await client.delete_workspace_share(args["workspace_id"], args["share_id"])
    if name == "create_cell_link":
        return await client.create_cell_link(
            args["target_sheet_id"], args["target_row_id"], args["target_column_id"],
            args["source_sheet_id"], args["source_row_id"], args["source_column_id"],
        )
    if name == "update_webhook":
        return await client.update_webhook(
            args["webhook_id"],
            enabled=args.get("enabled"),
            name=args.get("name"),
            events=args.get("events"),
            callback_url=args.get("callback_url"),
        )
    if name == "list_reports":
        return await client.list_reports()
    if name == "get_report":
        return await client.get_report(args["report_id"])
    if name == "list_dashboards":
        return await client.list_dashboards()
    if name == "get_dashboard":
        return await client.get_dashboard(args["dashboard_id"])
    if name == "generate_chart":
        return _generate_chart(args)
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


CHART_COLORS = [
    "rgba(59,130,246,0.7)", "rgba(139,92,246,0.7)", "rgba(16,185,129,0.7)",
    "rgba(245,158,11,0.7)", "rgba(239,68,68,0.7)", "rgba(236,72,153,0.7)",
    "rgba(99,102,241,0.7)", "rgba(20,184,166,0.7)", "rgba(234,179,8,0.7)",
    "rgba(168,85,247,0.7)",
]
CHART_BORDERS = [c.replace("0.7", "1") for c in CHART_COLORS]


def _generate_chart(args: dict) -> dict:
    chart_type = args.get("chart_type", "bar")
    title = args.get("title", "Chart")
    labels = args.get("labels", [])
    datasets = args.get("datasets", [])

    for i, ds in enumerate(datasets):
        if "backgroundColor" not in ds:
            if chart_type in ("pie", "doughnut", "polarArea"):
                ds["backgroundColor"] = CHART_COLORS[:len(labels)]
                ds["borderColor"] = CHART_BORDERS[:len(labels)]
            else:
                ds["backgroundColor"] = CHART_COLORS[i % len(CHART_COLORS)]
                ds["borderColor"] = CHART_BORDERS[i % len(CHART_BORDERS)]
        if "borderWidth" not in ds:
            ds["borderWidth"] = 1

    spec = {
        "type": chart_type,
        "data": {"labels": labels, "datasets": datasets},
        "options": {
            "responsive": True,
            "plugins": {"title": {"display": True, "text": title, "color": "#E8ECF4", "font": {"size": 14}}},
            "scales": {} if chart_type in ("pie", "doughnut", "polarArea", "radar") else {
                "x": {"ticks": {"color": "#8B95B0"}, "grid": {"color": "rgba(255,255,255,0.06)"}},
                "y": {"ticks": {"color": "#8B95B0"}, "grid": {"color": "rgba(255,255,255,0.06)"}},
            },
        },
    }

    return {"__is_chart__": True, "chart_spec": spec, "summary": f"Chart: {title} ({chart_type}, {len(labels)} labels, {len(datasets)} datasets)"}


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
