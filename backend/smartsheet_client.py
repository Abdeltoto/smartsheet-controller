import httpx
from typing import Any

BASE_URL = "https://api.smartsheet.com/2.0"


class SmartsheetClient:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(headers=self.headers, timeout=30.0)

    async def close(self):
        await self.client.aclose()

    # ── User / Account ────────────────────────────────────────

    async def get_current_user(self) -> dict:
        r = await self.client.get(f"{BASE_URL}/users/me")
        r.raise_for_status()
        data = r.json()
        return {
            "id": data["id"],
            "email": data.get("email"),
            "firstName": data.get("firstName"),
            "lastName": data.get("lastName"),
            "locale": data.get("locale"),
            "timeZone": data.get("timeZone"),
            "account": data.get("account"),
        }

    # ── Search ────────────────────────────────────────────────

    async def search_everything(self, query: str) -> dict:
        r = await self.client.get(f"{BASE_URL}/search", params={"query": query})
        r.raise_for_status()
        return r.json()

    async def search_sheet(self, sheet_id: str, query: str) -> dict:
        r = await self.client.get(f"{BASE_URL}/search/sheets/{sheet_id}", params={"query": query})
        r.raise_for_status()
        return r.json()

    # ── Sheet ────────────────────────────────────────────────

    async def get_sheet(self, sheet_id: str, page_size: int = 500) -> dict:
        r = await self.client.get(f"{BASE_URL}/sheets/{sheet_id}?pageSize={page_size}")
        r.raise_for_status()
        return r.json()

    async def get_sheet_summary(self, sheet_id: str) -> dict:
        sheet = await self.get_sheet(sheet_id, page_size=0)
        columns = [
            {"index": c["index"], "title": c["title"], "type": c["type"], "id": c["id"]}
            for c in sheet.get("columns", [])
        ]
        return {
            "name": sheet.get("name"),
            "totalRowCount": sheet.get("totalRowCount"),
            "columns": columns,
            "columnCount": len(columns),
        }

    async def list_sheets(self) -> list[dict]:
        r = await self.client.get(f"{BASE_URL}/sheets?includeAll=true")
        r.raise_for_status()
        return [{"id": s["id"], "name": s["name"]} for s in r.json().get("data", [])]

    async def create_sheet(self, name: str, columns: list[dict]) -> dict:
        body = {"name": name, "columns": columns}
        r = await self.client.post(f"{BASE_URL}/sheets", json=body)
        r.raise_for_status()
        return r.json()

    async def delete_sheet(self, sheet_id: str) -> dict:
        r = await self.client.delete(f"{BASE_URL}/sheets/{sheet_id}")
        r.raise_for_status()
        return r.json()

    async def rename_sheet(self, sheet_id: str, new_name: str) -> dict:
        r = await self.client.put(f"{BASE_URL}/sheets/{sheet_id}", json={"name": new_name})
        r.raise_for_status()
        return r.json()

    async def copy_sheet(self, sheet_id: str, new_name: str, destination_id: str | None = None, destination_type: str = "home") -> dict:
        body: dict[str, Any] = {"newName": new_name}
        if destination_id:
            body["destinationType"] = destination_type
            body["destinationId"] = int(destination_id)
        r = await self.client.post(f"{BASE_URL}/sheets/{sheet_id}/copy", json=body)
        r.raise_for_status()
        return r.json()

    async def move_sheet(self, sheet_id: str, destination_id: str, destination_type: str = "folder") -> dict:
        body = {"destinationType": destination_type, "destinationId": int(destination_id)}
        r = await self.client.post(f"{BASE_URL}/sheets/{sheet_id}/move", json=body)
        r.raise_for_status()
        return r.json()

    # ── Workspaces ────────────────────────────────────────────

    async def list_workspaces(self) -> list[dict]:
        r = await self.client.get(f"{BASE_URL}/workspaces")
        r.raise_for_status()
        return [{"id": w["id"], "name": w["name"]} for w in r.json().get("data", [])]

    async def get_workspace(self, workspace_id: str) -> dict:
        r = await self.client.get(f"{BASE_URL}/workspaces/{workspace_id}")
        r.raise_for_status()
        return r.json()

    # ── Folders ───────────────────────────────────────────────

    async def list_home_folders(self) -> list[dict]:
        r = await self.client.get(f"{BASE_URL}/home/folders")
        r.raise_for_status()
        return [{"id": f["id"], "name": f["name"]} for f in r.json().get("data", [])]

    async def get_folder(self, folder_id: str) -> dict:
        r = await self.client.get(f"{BASE_URL}/folders/{folder_id}")
        r.raise_for_status()
        return r.json()

    async def create_folder(self, name: str, parent_folder_id: str | None = None) -> dict:
        if parent_folder_id:
            url = f"{BASE_URL}/folders/{parent_folder_id}/folders"
        else:
            url = f"{BASE_URL}/home/folders"
        r = await self.client.post(url, json={"name": name})
        r.raise_for_status()
        return r.json()

    # ── Recent Items ──────────────────────────────────────────

    async def get_recent_items(self) -> dict:
        r = await self.client.get(f"{BASE_URL}/home")
        r.raise_for_status()
        return r.json()

    # ── Columns ──────────────────────────────────────────────

    async def add_column(self, sheet_id: str, title: str, col_type: str, index: int, description: str = "") -> dict:
        body: dict[str, Any] = {"title": title, "type": col_type, "index": index}
        if description:
            body["description"] = description[:250]
        r = await self.client.post(f"{BASE_URL}/sheets/{sheet_id}/columns", json=body)
        r.raise_for_status()
        return r.json()

    async def update_column(self, sheet_id: str, column_id: int, **kwargs) -> dict:
        r = await self.client.put(f"{BASE_URL}/sheets/{sheet_id}/columns/{column_id}", json=kwargs)
        r.raise_for_status()
        return r.json()

    async def delete_column(self, sheet_id: str, column_id: int) -> dict:
        r = await self.client.delete(f"{BASE_URL}/sheets/{sheet_id}/columns/{column_id}")
        r.raise_for_status()
        return r.json()

    # ── Rows ─────────────────────────────────────────────────

    async def get_rows(self, sheet_id: str, row_range: str | None = None) -> list[dict]:
        sheet = await self.get_sheet(sheet_id)
        columns = {c["id"]: c["title"] for c in sheet.get("columns", [])}
        rows = sheet.get("rows", [])

        if row_range:
            start, end = _parse_range(row_range)
            rows = [r for r in rows if start <= r["rowNumber"] <= end]

        result = []
        for row in rows:
            cells = {}
            for cell in row.get("cells", []):
                col_name = columns.get(cell["columnId"], str(cell["columnId"]))
                cells[col_name] = {
                    "value": cell.get("value"),
                    "displayValue": cell.get("displayValue"),
                    "formula": cell.get("formula"),
                }
            result.append({"rowNumber": row["rowNumber"], "rowId": row["id"], "cells": cells})
        return result

    async def update_rows(self, sheet_id: str, updates: list[dict]) -> dict:
        """
        updates: [{"rowId": int, "cells": {"ColumnName": {"value": ...} or {"formula": ...}}}]
        """
        sheet = await self.get_sheet(sheet_id, page_size=0)
        col_map = {c["title"]: c["id"] for c in sheet.get("columns", [])}

        api_rows = []
        for upd in updates:
            cells = []
            for col_name, cell_data in upd["cells"].items():
                col_id = col_map.get(col_name)
                if not col_id:
                    continue
                cell: dict[str, Any] = {"columnId": col_id}
                if "formula" in cell_data:
                    cell["formula"] = cell_data["formula"]
                elif "value" in cell_data:
                    cell["value"] = cell_data["value"]
                cells.append(cell)
            api_rows.append({"id": upd["rowId"], "cells": cells})

        r = await self.client.put(f"{BASE_URL}/sheets/{sheet_id}/rows", json=api_rows)
        r.raise_for_status()
        return r.json()

    async def add_rows(self, sheet_id: str, rows: list[dict], to_bottom: bool = True) -> dict:
        sheet = await self.get_sheet(sheet_id, page_size=0)
        col_map = {c["title"]: c["id"] for c in sheet.get("columns", [])}

        api_rows = []
        for row_data in rows:
            cells = []
            for col_name, cell_data in row_data.items():
                col_id = col_map.get(col_name)
                if not col_id:
                    continue
                cell: dict[str, Any] = {"columnId": col_id}
                if isinstance(cell_data, dict) and "formula" in cell_data:
                    cell["formula"] = cell_data["formula"]
                else:
                    cell["value"] = cell_data
                cells.append(cell)
            entry: dict[str, Any] = {"cells": cells}
            if to_bottom:
                entry["toBottom"] = True
            api_rows.append(entry)

        r = await self.client.post(f"{BASE_URL}/sheets/{sheet_id}/rows", json=api_rows)
        r.raise_for_status()
        return r.json()

    async def delete_rows(self, sheet_id: str, row_ids: list[int]) -> dict:
        ids_str = ",".join(str(i) for i in row_ids)
        r = await self.client.delete(f"{BASE_URL}/sheets/{sheet_id}/rows?ids={ids_str}")
        r.raise_for_status()
        return r.json()

    async def get_row(self, sheet_id: str, row_id: int) -> dict:
        r = await self.client.get(
            f"{BASE_URL}/sheets/{sheet_id}/rows/{row_id}",
            params={"include": "columns,format"},
        )
        r.raise_for_status()
        return r.json()

    async def move_rows(self, sheet_id: str, row_ids: list[int], dest_sheet_id: str) -> dict:
        body = {
            "rowIds": row_ids,
            "to": {"sheetId": int(dest_sheet_id)},
        }
        r = await self.client.post(f"{BASE_URL}/sheets/{sheet_id}/rows/move", json=body)
        r.raise_for_status()
        return r.json()

    async def copy_rows(self, sheet_id: str, row_ids: list[int], dest_sheet_id: str) -> dict:
        body = {
            "rowIds": row_ids,
            "to": {"sheetId": int(dest_sheet_id)},
        }
        r = await self.client.post(f"{BASE_URL}/sheets/{sheet_id}/rows/copy", json=body)
        r.raise_for_status()
        return r.json()

    async def sort_sheet(self, sheet_id: str, sort_criteria: list[dict]) -> dict:
        body = {"sortCriteria": sort_criteria}
        r = await self.client.post(f"{BASE_URL}/sheets/{sheet_id}/sort", json=body)
        r.raise_for_status()
        return r.json()

    # ── Cell History ──────────────────────────────────────────

    async def get_cell_history(self, sheet_id: str, row_id: int, column_id: int) -> list[dict]:
        r = await self.client.get(
            f"{BASE_URL}/sheets/{sheet_id}/rows/{row_id}/columns/{column_id}/history",
            params={"include": "columnType"},
        )
        r.raise_for_status()
        return r.json().get("data", [])

    # ── Summary Fields ────────────────────────────────────────

    async def get_sheet_summary_fields(self, sheet_id: str) -> list[dict]:
        r = await self.client.get(f"{BASE_URL}/sheets/{sheet_id}/summary")
        r.raise_for_status()
        return r.json().get("fields", [])

    # ── Sharing ───────────────────────────────────────────────

    async def list_shares(self, sheet_id: str) -> list[dict]:
        r = await self.client.get(f"{BASE_URL}/sheets/{sheet_id}/shares")
        r.raise_for_status()
        return r.json().get("data", [])

    async def share_sheet(self, sheet_id: str, email: str, access_level: str = "VIEWER", message: str = "") -> dict:
        body: list[dict[str, Any]] = [{"email": email, "accessLevel": access_level}]
        params: dict[str, Any] = {}
        if message:
            params["sendEmail"] = "true"
        r = await self.client.post(f"{BASE_URL}/sheets/{sheet_id}/shares", json=body, params=params)
        r.raise_for_status()
        return r.json()

    async def delete_share(self, sheet_id: str, share_id: str) -> dict:
        r = await self.client.delete(f"{BASE_URL}/sheets/{sheet_id}/shares/{share_id}")
        r.raise_for_status()
        return r.json()

    async def update_share(self, sheet_id: str, share_id: str, access_level: str) -> dict:
        r = await self.client.put(
            f"{BASE_URL}/sheets/{sheet_id}/shares/{share_id}",
            json={"accessLevel": access_level},
        )
        r.raise_for_status()
        return r.json()

    # ── Discussions & Comments ────────────────────────────────

    async def list_discussions(self, sheet_id: str) -> list[dict]:
        r = await self.client.get(
            f"{BASE_URL}/sheets/{sheet_id}/discussions",
            params={"include": "comments"},
        )
        r.raise_for_status()
        return r.json().get("data", [])

    async def list_row_discussions(self, sheet_id: str, row_id: int) -> list[dict]:
        r = await self.client.get(
            f"{BASE_URL}/sheets/{sheet_id}/rows/{row_id}/discussions",
            params={"include": "comments"},
        )
        r.raise_for_status()
        return r.json().get("data", [])

    async def add_comment(self, sheet_id: str, discussion_id: int, text: str) -> dict:
        r = await self.client.post(
            f"{BASE_URL}/sheets/{sheet_id}/discussions/{discussion_id}/comments",
            json={"text": text},
        )
        r.raise_for_status()
        return r.json()

    async def create_discussion_on_row(self, sheet_id: str, row_id: int, text: str) -> dict:
        body = {"comment": {"text": text}}
        r = await self.client.post(
            f"{BASE_URL}/sheets/{sheet_id}/rows/{row_id}/discussions",
            json=body,
        )
        r.raise_for_status()
        return r.json()

    # ── Attachments ───────────────────────────────────────────

    async def list_attachments(self, sheet_id: str) -> list[dict]:
        r = await self.client.get(f"{BASE_URL}/sheets/{sheet_id}/attachments")
        r.raise_for_status()
        return r.json().get("data", [])

    async def get_attachment(self, sheet_id: str, attachment_id: int) -> dict:
        r = await self.client.get(f"{BASE_URL}/sheets/{sheet_id}/attachments/{attachment_id}")
        r.raise_for_status()
        return r.json()

    # ── Reports ───────────────────────────────────────────────

    async def list_reports(self) -> list[dict]:
        r = await self.client.get(f"{BASE_URL}/reports")
        r.raise_for_status()
        return [{"id": rp["id"], "name": rp["name"]} for rp in r.json().get("data", [])]

    async def get_report(self, report_id: str, page_size: int = 500) -> dict:
        r = await self.client.get(f"{BASE_URL}/reports/{report_id}", params={"pageSize": page_size})
        r.raise_for_status()
        return r.json()

    # ── Dashboards (Sights) ───────────────────────────────────

    async def list_dashboards(self) -> list[dict]:
        r = await self.client.get(f"{BASE_URL}/sights")
        r.raise_for_status()
        return [{"id": d["id"], "name": d["name"]} for d in r.json().get("data", [])]

    async def get_dashboard(self, dashboard_id: str) -> dict:
        r = await self.client.get(f"{BASE_URL}/sights/{dashboard_id}")
        r.raise_for_status()
        return r.json()

    # ── Templates ─────────────────────────────────────────────

    async def list_public_templates(self) -> list[dict]:
        r = await self.client.get(f"{BASE_URL}/templates/public")
        r.raise_for_status()
        return r.json().get("data", [])

    # ── Webhooks ──────────────────────────────────────────────

    async def list_webhooks(self) -> list[dict]:
        r = await self.client.get(f"{BASE_URL}/webhooks")
        r.raise_for_status()
        return r.json().get("data", [])

    async def create_webhook(self, name: str, sheet_id: str, callback_url: str, events: list[str] | None = None) -> dict:
        body: dict[str, Any] = {
            "name": name,
            "callbackUrl": callback_url,
            "scope": "sheet",
            "scopeObjectId": int(sheet_id),
            "events": events or ["*.*"],
            "version": 1,
        }
        r = await self.client.post(f"{BASE_URL}/webhooks", json=body)
        r.raise_for_status()
        return r.json()

    async def delete_webhook(self, webhook_id: int) -> dict:
        r = await self.client.delete(f"{BASE_URL}/webhooks/{webhook_id}")
        r.raise_for_status()
        return r.json()

    # ── Cross-sheet References ───────────────────────────────

    async def list_cross_sheet_refs(self, sheet_id: str) -> list[dict]:
        r = await self.client.get(f"{BASE_URL}/sheets/{sheet_id}/crosssheetreferences")
        r.raise_for_status()
        return r.json().get("data", [])

    async def create_cross_sheet_ref(self, sheet_id: str, name: str, source_sheet_id: int, start_col_id: int, end_col_id: int) -> dict:
        body = {
            "name": name,
            "sourceSheetId": source_sheet_id,
            "startColumnId": start_col_id,
            "endColumnId": end_col_id,
        }
        r = await self.client.post(f"{BASE_URL}/sheets/{sheet_id}/crosssheetreferences", json=body)
        r.raise_for_status()
        return r.json()

    # ── Automations ──────────────────────────────────────────

    async def list_automations(self, sheet_id: str) -> list[dict]:
        r = await self.client.get(f"{BASE_URL}/sheets/{sheet_id}/automationrules")
        r.raise_for_status()
        return r.json().get("data", [])

    # ── Deep Analysis ────────────────────────────────────────

    async def analyze_sheet(self, sheet_id: str) -> dict:
        sheet = await self.get_sheet(sheet_id)
        columns = sheet.get("columns", [])
        rows = sheet.get("rows", [])
        col_map = {c["id"]: c for c in columns}

        col_info = []
        for c in columns:
            col_info.append({
                "index": c["index"],
                "title": c["title"],
                "type": c["type"],
                "id": c["id"],
                "description": c.get("description", ""),
            })

        sections = []
        current_section = {"start": 1, "rows": [], "label": ""}
        formula_columns = set()
        crossref_columns = set()
        value_columns = set()

        for row in rows:
            rn = row["rowNumber"]
            row_data = {"rowNumber": rn, "rowId": row["id"], "cells": {}}
            is_empty = True
            for cell in row.get("cells", []):
                col = col_map.get(cell["columnId"], {})
                col_name = col.get("title", str(cell["columnId"]))
                val = cell.get("value")
                formula = cell.get("formula")
                display = cell.get("displayValue")
                if val is not None or formula:
                    is_empty = False
                    row_data["cells"][col_name] = {
                        "value": display or val,
                    }
                    if formula:
                        row_data["cells"][col_name]["formula"] = formula
                        formula_columns.add(col_name)
                        if "{" in formula:
                            crossref_columns.add(col_name)
                    else:
                        value_columns.add(col_name)

            if not is_empty:
                current_section["rows"].append(row_data)
            elif current_section["rows"] and len([r for r in current_section["rows"]]) > 0:
                empty_streak = 0
                for future_row in rows[rows.index(row):]:
                    future_empty = all(
                        not c.get("value") and not c.get("formula")
                        for c in future_row.get("cells", [])
                    )
                    if future_empty:
                        empty_streak += 1
                    else:
                        break
                if empty_streak >= 3:
                    current_section["end"] = rn - 1
                    sections.append(current_section)
                    current_section = {"start": rn + empty_streak, "rows": [], "label": ""}
                else:
                    current_section["rows"].append(row_data)

        if current_section["rows"]:
            current_section["end"] = rows[-1]["rowNumber"] if rows else 0
            sections.append(current_section)

        xrefs = await self.list_cross_sheet_refs(sheet_id)
        automations = await self.list_automations(sheet_id)

        distinct_values: dict[str, list] = {}
        for col in columns:
            if col["type"] == "PICKLIST":
                vals = set()
                for row in rows:
                    for cell in row.get("cells", []):
                        if cell["columnId"] == col["id"] and cell.get("value"):
                            vals.add(str(cell["value"]))
                if vals:
                    distinct_values[col["title"]] = sorted(vals)

        return {
            "name": sheet.get("name"),
            "totalRows": len(rows),
            "columns": col_info,
            "sections": [
                {
                    "rows": f"{s['start']}-{s.get('end', '?')}",
                    "rowCount": len(s["rows"]),
                    "sample_first_row": s["rows"][0] if s["rows"] else None,
                    "sample_last_row": s["rows"][-1] if s["rows"] else None,
                }
                for s in sections
            ],
            "formula_columns": sorted(formula_columns),
            "crossref_columns": sorted(crossref_columns),
            "manual_input_columns": sorted(value_columns - formula_columns),
            "cross_sheet_references": [
                {"name": x["name"], "sourceSheetId": x.get("sourceSheetId")}
                for x in xrefs
            ],
            "automations": [
                {"name": a["name"], "enabled": a.get("enabled"), "type": a.get("action", {}).get("type")}
                for a in automations
            ],
            "picklist_values": distinct_values,
            "all_rows": [
                {
                    "rowNumber": r["rowNumber"],
                    "rowId": r["id"],
                    "cells": {
                        col_map.get(c["columnId"], {}).get("title", ""): {
                            k: v for k, v in {
                                "value": c.get("displayValue") or c.get("value"),
                                "formula": c.get("formula"),
                            }.items() if v
                        }
                        for c in r.get("cells", [])
                        if c.get("value") is not None or c.get("formula")
                    },
                }
                for r in rows
                if any(c.get("value") is not None or c.get("formula") for c in r.get("cells", []))
            ],
        }

    async def detect_issues(self, sheet_id: str) -> dict:
        sheet = await self.get_sheet(sheet_id)
        columns = sheet.get("columns", [])
        rows = sheet.get("rows", [])
        col_map = {c["id"]: c for c in columns}

        issues: list[dict] = []

        cols_without_desc = [c["title"] for c in columns if not c.get("description")]
        if cols_without_desc:
            issues.append({
                "type": "missing_descriptions",
                "severity": "low",
                "message": f"{len(cols_without_desc)} columns have no description",
                "details": cols_without_desc,
            })

        error_cells = []
        for row in rows:
            for cell in row.get("cells", []):
                val = cell.get("value")
                if isinstance(val, str) and val.startswith("#"):
                    col_name = col_map.get(cell["columnId"], {}).get("title", "?")
                    error_cells.append(f"Row {row['rowNumber']}, {col_name}: {val}")
        if error_cells:
            issues.append({
                "type": "formula_errors",
                "severity": "high",
                "message": f"{len(error_cells)} cells have formula errors",
                "details": error_cells[:20],
            })

        empty_cols = []
        for col in columns:
            has_data = False
            for row in rows:
                for cell in row.get("cells", []):
                    if cell["columnId"] == col["id"] and (cell.get("value") is not None or cell.get("formula")):
                        has_data = True
                        break
                if has_data:
                    break
            if not has_data:
                empty_cols.append(col["title"])
        if empty_cols:
            issues.append({
                "type": "empty_columns",
                "severity": "medium",
                "message": f"{len(empty_cols)} columns contain no data",
                "details": empty_cols,
            })

        for col in columns:
            if col["type"] == "PICKLIST":
                vals = set()
                for row in rows:
                    for cell in row.get("cells", []):
                        if cell["columnId"] == col["id"] and cell.get("value"):
                            vals.add(str(cell["value"]))
                similar_pairs = []
                val_list = list(vals)
                for i, a in enumerate(val_list):
                    for b in val_list[i + 1:]:
                        if a.lower() == b.lower() and a != b:
                            similar_pairs.append(f"'{a}' vs '{b}'")
                if similar_pairs:
                    issues.append({
                        "type": "inconsistent_values",
                        "severity": "medium",
                        "message": f"Column '{col['title']}' has case-inconsistent values",
                        "details": similar_pairs,
                    })

        return {
            "total_issues": len(issues),
            "issues": issues,
        }


def _parse_range(range_str: str) -> tuple[int, int]:
    parts = range_str.split("-")
    if len(parts) == 2:
        return int(parts[0].strip()), int(parts[1].strip())
    n = int(parts[0].strip())
    return n, n
