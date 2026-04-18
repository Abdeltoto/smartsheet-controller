import asyncio
import logging
import time
import httpx
from typing import Any

BASE_URL = "https://api.smartsheet.com/2.0"
SCHEMA_CACHE_TTL = 30.0  # seconds — column maps used 4-5×/turn

log = logging.getLogger(__name__)


class SmartsheetRateLimitError(Exception):
    """Smartsheet 429 that persisted after retries."""


class SmartsheetClient:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self.client = httpx.AsyncClient(headers=self.headers, timeout=30.0)
        # Schema cache: sheet_id -> (expires_at, data). Only for page_size=0.
        self._schema_cache: dict[str, tuple[float, dict]] = {}
        self._cache_hits = 0
        self._cache_misses = 0

    async def close(self):
        await self.client.aclose()

    def invalidate_schema_cache(self, sheet_id: str | None = None) -> None:
        """Drop cached schemas (call after writes that may change columns)."""
        if sheet_id is None:
            self._schema_cache.clear()
        else:
            self._schema_cache.pop(str(sheet_id), None)

    def cache_stats(self) -> dict:
        total = self._cache_hits + self._cache_misses
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "hit_rate": (self._cache_hits / total) if total else 0.0,
            "size": len(self._schema_cache),
        }

    async def _request(self, method: str, url: str, *, max_retries: int = 3, **kwargs) -> httpx.Response:
        """Request with exponential backoff on 429 and 5xx."""
        attempt = 0
        while True:
            try:
                r = await self.client.request(method, url, **kwargs)
            except httpx.RequestError as e:
                if attempt >= max_retries:
                    raise
                attempt += 1
                await asyncio.sleep(min(2 ** attempt, 8))
                log.warning(f"Smartsheet network error (attempt {attempt}): {e}")
                continue

            if r.status_code == 429 or (500 <= r.status_code < 600):
                if attempt >= max_retries:
                    if r.status_code == 429:
                        raise SmartsheetRateLimitError(
                            "Smartsheet is rate-limiting requests. Please wait a moment and try again."
                        )
                    r.raise_for_status()
                retry_after_hdr = r.headers.get("Retry-After")
                try:
                    retry_after = float(retry_after_hdr) if retry_after_hdr else 2 ** attempt
                except ValueError:
                    retry_after = 2 ** attempt
                retry_after = min(retry_after, 10.0)
                attempt += 1
                log.warning(f"Smartsheet {r.status_code} (attempt {attempt}), retrying in {retry_after:.1f}s")
                await asyncio.sleep(retry_after)
                continue

            return r

    # ── User / Account ────────────────────────────────────────

    async def get_current_user(self) -> dict:
        r = await self._request("GET", f"{BASE_URL}/users/me")
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
        r = await self._request("GET", f"{BASE_URL}/search", params={"query": query})
        r.raise_for_status()
        return r.json()

    async def search_sheet(self, sheet_id: str, query: str) -> dict:
        r = await self._request("GET", f"{BASE_URL}/search/sheets/{sheet_id}", params={"query": query})
        r.raise_for_status()
        return r.json()

    # ── Sheet ────────────────────────────────────────────────

    async def get_sheet(self, sheet_id: str, page_size: int = 500, max_rows: int = 5000) -> dict:
        # Cache schema-only calls (page_size=0) — column map fetched 4-5×/turn
        if page_size == 0:
            sid = str(sheet_id)
            entry = self._schema_cache.get(sid)
            now = time.monotonic()
            if entry and entry[0] > now:
                self._cache_hits += 1
                return entry[1]
            self._cache_misses += 1
            r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}", params={"pageSize": 0, "page": 1})
            r.raise_for_status()
            data = r.json()
            self._schema_cache[sid] = (now + SCHEMA_CACHE_TTL, data)
            return data
        r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}", params={"pageSize": page_size, "page": 1})
        r.raise_for_status()
        data = r.json()

        total = data.get("totalRowCount", 0)
        rows = data.get("rows", [])

        page = 2
        while len(rows) < total and len(rows) < max_rows:
            r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}", params={"pageSize": page_size, "page": page})
            r.raise_for_status()
            next_data = r.json()
            next_rows = next_data.get("rows", [])
            if not next_rows:
                break
            rows.extend(next_rows)
            page += 1

        data["rows"] = rows[:max_rows]
        data["_loadedRows"] = len(data["rows"])
        return data

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
        r = await self._request("GET", f"{BASE_URL}/sheets?includeAll=true")
        r.raise_for_status()
        return [{"id": s["id"], "name": s["name"]} for s in r.json().get("data", [])]

    async def create_sheet(self, name: str, columns: list[dict]) -> dict:
        body = {"name": name, "columns": columns}
        r = await self._request("POST", f"{BASE_URL}/sheets", json=body)
        r.raise_for_status()
        return r.json()

    async def delete_sheet(self, sheet_id: str) -> dict:
        r = await self._request("DELETE", f"{BASE_URL}/sheets/{sheet_id}")
        r.raise_for_status()
        self.invalidate_schema_cache(sheet_id)
        return r.json()

    async def rename_sheet(self, sheet_id: str, new_name: str) -> dict:
        r = await self._request("PUT", f"{BASE_URL}/sheets/{sheet_id}", json={"name": new_name})
        r.raise_for_status()
        self.invalidate_schema_cache(sheet_id)
        return r.json()

    async def copy_sheet(self, sheet_id: str, new_name: str, destination_id: str | None = None, destination_type: str = "home") -> dict:
        body: dict[str, Any] = {"newName": new_name}
        if destination_id:
            body["destinationType"] = destination_type
            body["destinationId"] = int(destination_id)
        r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/copy", json=body)
        r.raise_for_status()
        return r.json()

    async def move_sheet(self, sheet_id: str, destination_id: str, destination_type: str = "folder") -> dict:
        body = {"destinationType": destination_type, "destinationId": int(destination_id)}
        r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/move", json=body)
        r.raise_for_status()
        return r.json()

    # ── Workspaces ────────────────────────────────────────────

    async def list_workspaces(self) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/workspaces")
        r.raise_for_status()
        return [{"id": w["id"], "name": w["name"]} for w in r.json().get("data", [])]

    async def get_workspace(self, workspace_id: str) -> dict:
        r = await self._request("GET", f"{BASE_URL}/workspaces/{workspace_id}")
        r.raise_for_status()
        return r.json()

    # ── Folders ───────────────────────────────────────────────

    async def list_home_folders(self) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/home/folders")
        r.raise_for_status()
        return [{"id": f["id"], "name": f["name"]} for f in r.json().get("data", [])]

    async def get_folder(self, folder_id: str) -> dict:
        r = await self._request("GET", f"{BASE_URL}/folders/{folder_id}")
        r.raise_for_status()
        return r.json()

    async def create_folder(self, name: str, parent_folder_id: str | None = None) -> dict:
        if parent_folder_id:
            url = f"{BASE_URL}/folders/{parent_folder_id}/folders"
        else:
            url = f"{BASE_URL}/home/folders"
        r = await self._request("POST", url, json={"name": name})
        r.raise_for_status()
        return r.json()

    # ── Recent Items ──────────────────────────────────────────

    async def get_recent_items(self) -> dict:
        r = await self._request("GET", f"{BASE_URL}/home")
        r.raise_for_status()
        return r.json()

    # ── Columns ──────────────────────────────────────────────

    async def add_column(self, sheet_id: str, title: str, col_type: str, index: int, description: str = "") -> dict:
        body: dict[str, Any] = {"title": title, "type": col_type, "index": index}
        if description:
            body["description"] = description[:250]
        r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/columns", json=body)
        r.raise_for_status()
        self.invalidate_schema_cache(sheet_id)
        return r.json()

    async def update_column(self, sheet_id: str, column_id: int, **kwargs) -> dict:
        r = await self._request("PUT", f"{BASE_URL}/sheets/{sheet_id}/columns/{column_id}", json=kwargs)
        r.raise_for_status()
        self.invalidate_schema_cache(sheet_id)
        return r.json()

    async def delete_column(self, sheet_id: str, column_id: int) -> dict:
        r = await self._request("DELETE", f"{BASE_URL}/sheets/{sheet_id}/columns/{column_id}")
        r.raise_for_status()
        self.invalidate_schema_cache(sheet_id)
        return r.json()

    # ── Rows ─────────────────────────────────────────────────

    async def get_rows(self, sheet_id: str, row_range: str | None = None, max_rows: int = 500) -> list[dict]:
        sheet = await self.get_sheet(sheet_id, max_rows=min(max_rows, 5000))
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

        r = await self._request("PUT", f"{BASE_URL}/sheets/{sheet_id}/rows", json=api_rows)
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

        r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/rows", json=api_rows)
        r.raise_for_status()
        return r.json()

    async def delete_rows(self, sheet_id: str, row_ids: list[int]) -> dict:
        ids_str = ",".join(str(i) for i in row_ids)
        r = await self._request("DELETE", f"{BASE_URL}/sheets/{sheet_id}/rows?ids={ids_str}")
        r.raise_for_status()
        return r.json()

    async def get_row(self, sheet_id: str, row_id: int) -> dict:
        r = await self._request("GET", 
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
        r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/rows/move", json=body)
        r.raise_for_status()
        return r.json()

    async def copy_rows(self, sheet_id: str, row_ids: list[int], dest_sheet_id: str) -> dict:
        body = {
            "rowIds": row_ids,
            "to": {"sheetId": int(dest_sheet_id)},
        }
        r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/rows/copy", json=body)
        r.raise_for_status()
        return r.json()

    async def sort_sheet(self, sheet_id: str, sort_criteria: list[dict]) -> dict:
        body = {"sortCriteria": sort_criteria}
        r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/sort", json=body)
        r.raise_for_status()
        return r.json()

    # ── Cell History ──────────────────────────────────────────

    async def get_cell_history(self, sheet_id: str, row_id: int, column_id: int) -> list[dict]:
        r = await self._request("GET", 
            f"{BASE_URL}/sheets/{sheet_id}/rows/{row_id}/columns/{column_id}/history",
            params={"include": "columnType"},
        )
        r.raise_for_status()
        return r.json().get("data", [])

    # ── Summary Fields ────────────────────────────────────────

    async def get_sheet_summary_fields(self, sheet_id: str) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}/summary")
        r.raise_for_status()
        return r.json().get("fields", [])

    # ── Sharing ───────────────────────────────────────────────

    async def list_shares(self, sheet_id: str) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}/shares")
        r.raise_for_status()
        return r.json().get("data", [])

    async def share_sheet(self, sheet_id: str, email: str, access_level: str = "VIEWER", message: str = "") -> dict:
        body: list[dict[str, Any]] = [{"email": email, "accessLevel": access_level}]
        params: dict[str, Any] = {}
        if message:
            params["sendEmail"] = "true"
        r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/shares", json=body, params=params)
        r.raise_for_status()
        return r.json()

    async def delete_share(self, sheet_id: str, share_id: str) -> dict:
        r = await self._request("DELETE", f"{BASE_URL}/sheets/{sheet_id}/shares/{share_id}")
        r.raise_for_status()
        return r.json()

    async def update_share(self, sheet_id: str, share_id: str, access_level: str) -> dict:
        r = await self._request("PUT", 
            f"{BASE_URL}/sheets/{sheet_id}/shares/{share_id}",
            json={"accessLevel": access_level},
        )
        r.raise_for_status()
        return r.json()

    # ── Discussions & Comments ────────────────────────────────

    async def list_discussions(self, sheet_id: str) -> list[dict]:
        r = await self._request("GET", 
            f"{BASE_URL}/sheets/{sheet_id}/discussions",
            params={"include": "comments"},
        )
        r.raise_for_status()
        return r.json().get("data", [])

    async def list_row_discussions(self, sheet_id: str, row_id: int) -> list[dict]:
        r = await self._request("GET", 
            f"{BASE_URL}/sheets/{sheet_id}/rows/{row_id}/discussions",
            params={"include": "comments"},
        )
        r.raise_for_status()
        return r.json().get("data", [])

    async def add_comment(self, sheet_id: str, discussion_id: int, text: str) -> dict:
        r = await self._request("POST", 
            f"{BASE_URL}/sheets/{sheet_id}/discussions/{discussion_id}/comments",
            json={"text": text},
        )
        r.raise_for_status()
        return r.json()

    async def create_discussion_on_row(self, sheet_id: str, row_id: int, text: str) -> dict:
        body = {"comment": {"text": text}}
        r = await self._request("POST", 
            f"{BASE_URL}/sheets/{sheet_id}/rows/{row_id}/discussions",
            json=body,
        )
        r.raise_for_status()
        return r.json()

    # ── Attachments ───────────────────────────────────────────

    async def list_attachments(self, sheet_id: str) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}/attachments")
        r.raise_for_status()
        return r.json().get("data", [])

    async def get_attachment(self, sheet_id: str, attachment_id: int) -> dict:
        r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}/attachments/{attachment_id}")
        r.raise_for_status()
        return r.json()

    async def list_row_attachments(self, sheet_id: str, row_id: int) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}/rows/{row_id}/attachments")
        r.raise_for_status()
        return r.json().get("data", [])

    async def attach_url_to_sheet(self, sheet_id: str, name: str, url: str,
                                   attachment_type: str = "LINK", description: str = "") -> dict:
        """Attach a URL/link to a sheet (no binary upload). attachment_type:
        LINK | GOOGLE_DRIVE | DROPBOX | BOX_COM | EVERNOTE | EGNYTE | ONEDRIVE."""
        body: dict[str, Any] = {"name": name, "url": url, "attachmentType": attachment_type}
        if description:
            body["description"] = description
        r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/attachments", json=body)
        r.raise_for_status()
        return r.json()

    async def attach_url_to_row(self, sheet_id: str, row_id: int, name: str, url: str,
                                 attachment_type: str = "LINK", description: str = "") -> dict:
        body: dict[str, Any] = {"name": name, "url": url, "attachmentType": attachment_type}
        if description:
            body["description"] = description
        r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/rows/{row_id}/attachments", json=body)
        r.raise_for_status()
        return r.json()

    async def upload_file_to_sheet(self, sheet_id: str, filename: str, content: bytes,
                                    content_type: str = "application/octet-stream") -> dict:
        """Binary upload of a file as attachment to a sheet. Smartsheet expects a raw body
        with Content-Disposition + Content-Length, NOT multipart."""
        url = f"{BASE_URL}/sheets/{sheet_id}/attachments"
        headers = {
            "Authorization": self.headers["Authorization"],
            "Content-Type": content_type,
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(content)),
        }
        r = await self.client.post(url, content=content, headers=headers)
        r.raise_for_status()
        return r.json()

    async def upload_file_to_row(self, sheet_id: str, row_id: int, filename: str, content: bytes,
                                  content_type: str = "application/octet-stream") -> dict:
        url = f"{BASE_URL}/sheets/{sheet_id}/rows/{row_id}/attachments"
        headers = {
            "Authorization": self.headers["Authorization"],
            "Content-Type": content_type,
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(content)),
        }
        r = await self.client.post(url, content=content, headers=headers)
        r.raise_for_status()
        return r.json()

    async def delete_attachment(self, sheet_id: str, attachment_id: int) -> dict:
        r = await self._request("DELETE", f"{BASE_URL}/sheets/{sheet_id}/attachments/{attachment_id}")
        r.raise_for_status()
        return r.json()

    # ── Forms ─────────────────────────────────────────────────

    async def list_sheet_forms(self, sheet_id: str) -> dict:
        """Smartsheet's public API does not expose a documented forms endpoint as of 2026-04.
        We try GET /sheets/{id}/forms (undocumented but sometimes available) and fall back
        to surfacing the sheet permalink so the user can manage forms via the UI."""
        try:
            r = await self.client.get(f"{BASE_URL}/sheets/{sheet_id}/forms")
            if r.status_code == 200:
                data = r.json()
                forms = data.get("data", data)
                return {"available_via_api": True, "forms": forms}
        except httpx.HTTPError:
            pass
        # Fallback: return the sheet permalink so the user knows where to manage forms.
        try:
            r2 = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}",
                                     params={"include": "ownerInfo", "pageSize": 0})
            r2.raise_for_status()
            j = r2.json()
            return {
                "available_via_api": False,
                "message": "Smartsheet's public API does not expose form listing. Manage forms in the Smartsheet UI under the Forms tab.",
                "permalink": j.get("permalink"),
                "sheet_name": j.get("name"),
            }
        except httpx.HTTPError as e:
            return {"available_via_api": False, "error": str(e)}

    # ── Reports ───────────────────────────────────────────────

    async def list_reports(self) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/reports")
        r.raise_for_status()
        return [{"id": rp["id"], "name": rp["name"]} for rp in r.json().get("data", [])]

    async def get_report(self, report_id: str, page_size: int = 500) -> dict:
        r = await self._request("GET", f"{BASE_URL}/reports/{report_id}", params={"pageSize": page_size})
        r.raise_for_status()
        return r.json()

    # ── Dashboards (Sights) ───────────────────────────────────

    async def list_dashboards(self) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/sights")
        r.raise_for_status()
        return [{"id": d["id"], "name": d["name"]} for d in r.json().get("data", [])]

    async def get_dashboard(self, dashboard_id: str) -> dict:
        r = await self._request("GET", f"{BASE_URL}/sights/{dashboard_id}")
        r.raise_for_status()
        return r.json()

    # ── Templates ─────────────────────────────────────────────

    async def list_public_templates(self) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/templates/public")
        r.raise_for_status()
        return r.json().get("data", [])

    # ── Webhooks ──────────────────────────────────────────────

    async def list_webhooks(self) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/webhooks")
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
        r = await self._request("POST", f"{BASE_URL}/webhooks", json=body)
        r.raise_for_status()
        return r.json()

    async def delete_webhook(self, webhook_id: int) -> dict:
        r = await self._request("DELETE", f"{BASE_URL}/webhooks/{webhook_id}")
        r.raise_for_status()
        return r.json()

    async def update_webhook(self, webhook_id: int, *, enabled: bool | None = None,
                             name: str | None = None, events: list[str] | None = None,
                             callback_url: str | None = None,
                             version: int | None = None) -> dict:
        """Enable/disable or reconfigure a webhook in place (avoids delete+recreate)."""
        body: dict[str, Any] = {}
        if enabled is not None:
            body["enabled"] = bool(enabled)
        if name is not None:
            body["name"] = name
        if events is not None:
            body["events"] = events
        if callback_url is not None:
            body["callbackUrl"] = callback_url
        if version is not None:
            body["version"] = version
        if not body:
            return {"warning": "No fields provided to update."}
        r = await self._request("PUT", f"{BASE_URL}/webhooks/{webhook_id}", json=body)
        r.raise_for_status()
        return r.json()

    # ── Cross-sheet References ───────────────────────────────

    async def list_cross_sheet_refs(self, sheet_id: str) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}/crosssheetreferences")
        r.raise_for_status()
        return r.json().get("data", [])

    async def create_cross_sheet_ref(self, sheet_id: str, name: str, source_sheet_id: int, start_col_id: int, end_col_id: int) -> dict:
        body = {
            "name": name,
            "sourceSheetId": source_sheet_id,
            "startColumnId": start_col_id,
            "endColumnId": end_col_id,
        }
        r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/crosssheetreferences", json=body)
        r.raise_for_status()
        return r.json()

    # ── Automations ──────────────────────────────────────────

    async def list_automations(self, sheet_id: str) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}/automationrules")
        r.raise_for_status()
        return r.json().get("data", [])

    async def get_automation(self, sheet_id: str, rule_id: int) -> dict:
        r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}/automationrules/{rule_id}")
        r.raise_for_status()
        return r.json()

    async def update_automation(self, sheet_id: str, rule_id: int, *,
                                 enabled: bool | None = None, name: str | None = None,
                                 action: dict | None = None) -> dict:
        """Enable/disable or rename an existing automation rule. Note: Smartsheet's API
        does NOT support creating new automation rules — they must be created in the UI."""
        body: dict[str, Any] = {}
        if enabled is not None:
            body["enabled"] = bool(enabled)
        if name is not None:
            body["name"] = name
        if action is not None:
            body["action"] = action
        if not body:
            return {"warning": "No fields provided to update."}
        r = await self._request("PUT", f"{BASE_URL}/sheets/{sheet_id}/automationrules/{rule_id}", json=body)
        r.raise_for_status()
        return r.json()

    async def delete_automation(self, sheet_id: str, rule_id: int) -> dict:
        r = await self._request("DELETE", f"{BASE_URL}/sheets/{sheet_id}/automationrules/{rule_id}")
        r.raise_for_status()
        return r.json()

    # ── Proofs (premium feature) ──────────────────────────────

    async def list_row_proofs(self, sheet_id: str, row_id: int) -> dict:
        try:
            r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}/rows/{row_id}/proofs")
            r.raise_for_status()
            return {"available": True, "proofs": r.json().get("data", [])}
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (403, 404):
                return {"available": False,
                        "message": "Proofs are a Smartsheet Premium feature. Your account or this sheet may not have it enabled.",
                        "status": e.response.status_code}
            raise

    async def create_row_proof_from_url(self, sheet_id: str, row_id: int, name: str,
                                         url: str, version_name: str = "v1") -> dict:
        body = {
            "name": name,
            "originalAttachment": {
                "name": name,
                "url": url,
                "attachmentType": "LINK",
            },
            "versionName": version_name,
        }
        try:
            r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/rows/{row_id}/proofs", json=body)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (403, 404):
                return {"available": False,
                        "message": "Proofs are a Smartsheet Premium feature. Your account or this sheet may not have it enabled."}
            raise

    # ── Update Requests ──────────────────────────────────────

    async def list_update_requests(self, sheet_id: str) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/sheets/{sheet_id}/updaterequests")
        r.raise_for_status()
        return r.json().get("data", [])

    async def create_update_request(self, sheet_id: str, *, send_to_emails: list[str],
                                     row_ids: list[int], column_ids: list[int] | None = None,
                                     subject: str | None = None, message: str | None = None,
                                     cc_me: bool = False, include_attachments: bool = False,
                                     include_discussions: bool = False) -> dict:
        body: dict[str, Any] = {
            "sendTo": [{"email": e} for e in send_to_emails],
            "rowIds": [int(r) for r in row_ids],
            "ccMe": bool(cc_me),
            "includeAttachments": bool(include_attachments),
            "includeDiscussions": bool(include_discussions),
        }
        if column_ids:
            body["columnIds"] = [int(c) for c in column_ids]
        if subject:
            body["subject"] = subject
        if message:
            body["message"] = message
        r = await self._request("POST", f"{BASE_URL}/sheets/{sheet_id}/updaterequests", json=body)
        r.raise_for_status()
        return r.json()

    async def delete_update_request(self, sheet_id: str, update_request_id: int) -> dict:
        r = await self._request("DELETE", f"{BASE_URL}/sheets/{sheet_id}/updaterequests/{update_request_id}")
        r.raise_for_status()
        return r.json()

    # ── Workspace sharing ────────────────────────────────────

    async def list_workspace_shares(self, workspace_id: str) -> list[dict]:
        r = await self._request("GET", f"{BASE_URL}/workspaces/{workspace_id}/shares")
        r.raise_for_status()
        return r.json().get("data", [])

    async def share_workspace(self, workspace_id: str, email: str,
                              access_level: str = "VIEWER", message: str = "") -> dict:
        body: list[dict[str, Any]] = [{"email": email, "accessLevel": access_level}]
        params: dict[str, Any] = {}
        if message:
            params["sendEmail"] = "true"
        r = await self._request("POST", f"{BASE_URL}/workspaces/{workspace_id}/shares",
                                json=body, params=params)
        r.raise_for_status()
        return r.json()

    async def update_workspace_share(self, workspace_id: str, share_id: str,
                                      access_level: str) -> dict:
        r = await self._request("PUT", f"{BASE_URL}/workspaces/{workspace_id}/shares/{share_id}",
                                json={"accessLevel": access_level})
        r.raise_for_status()
        return r.json()

    async def delete_workspace_share(self, workspace_id: str, share_id: str) -> dict:
        r = await self._request("DELETE", f"{BASE_URL}/workspaces/{workspace_id}/shares/{share_id}")
        r.raise_for_status()
        return r.json()

    # ── Cell linking (distinct from cross-sheet refs) ────────

    async def create_cell_link(self, target_sheet_id: str, target_row_id: int, target_column_id: int,
                                source_sheet_id: int, source_row_id: int, source_column_id: int) -> dict:
        """Create a one-way live cell link: target cell receives data from source cell.
        This is distinct from cross-sheet references (which are formula ingredients)."""
        body = [{
            "id": int(target_row_id),
            "cells": [{
                "columnId": int(target_column_id),
                "value": "",
                "linkInFromCell": {
                    "sheetId": int(source_sheet_id),
                    "rowId": int(source_row_id),
                    "columnId": int(source_column_id),
                },
            }],
        }]
        r = await self._request("PUT", f"{BASE_URL}/sheets/{target_sheet_id}/rows", json=body)
        r.raise_for_status()
        return r.json()

    # ── Deep Analysis ────────────────────────────────────────

    @staticmethod
    def _sample_rows(rows: list[dict], max_sample: int = 1000) -> tuple[list[dict], dict | None]:
        """Smart sampling for large sheets: first N + last N + random middle.
        Returns (sampled_rows, sampling_info_or_None)."""
        n = len(rows)
        if n <= max_sample:
            return rows, None
        import random
        # 40% head + 40% tail + 20% random middle
        head_n = int(max_sample * 0.4)
        tail_n = int(max_sample * 0.4)
        mid_n = max_sample - head_n - tail_n
        head = rows[:head_n]
        tail = rows[-tail_n:]
        middle_pool = rows[head_n:n - tail_n]
        rng = random.Random(42)  # deterministic for reproducibility
        mid_sample = rng.sample(middle_pool, min(mid_n, len(middle_pool))) if middle_pool else []
        # Re-sort by rowNumber to keep coherent ordering
        sampled = sorted(head + mid_sample + tail, key=lambda r: r.get("rowNumber", 0))
        info = {
            "total_rows": n,
            "sampled_rows": len(sampled),
            "strategy": f"first {len(head)} + random {len(mid_sample)} from middle + last {len(tail)}",
            "warning": f"Large sheet ({n:,} rows) — analysis based on a {len(sampled)}-row sample. Use read_rows for exact data.",
        }
        return sampled, info

    async def analyze_sheet(self, sheet_id: str, max_sample: int = 1000) -> dict:
        sheet = await self.get_sheet(sheet_id)
        columns = sheet.get("columns", [])
        all_rows = sheet.get("rows", [])
        rows, sampling_info = self._sample_rows(all_rows, max_sample=max_sample)
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

        result = {
            "name": sheet.get("name"),
            "totalRows": len(all_rows),
            "analyzedRows": len(rows),
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
        if sampling_info:
            result["sampling"] = sampling_info
        return result

    async def detect_issues(self, sheet_id: str, max_sample: int = 1000) -> dict:
        sheet = await self.get_sheet(sheet_id)
        columns = sheet.get("columns", [])
        all_rows = sheet.get("rows", [])
        rows, sampling_info = self._sample_rows(all_rows, max_sample=max_sample)
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

        result = {
            "total_issues": len(issues),
            "issues": issues,
        }
        if sampling_info:
            result["sampling"] = sampling_info
        return result


def _parse_range(range_str: str) -> tuple[int, int]:
    parts = range_str.split("-")
    if len(parts) == 2:
        return int(parts[0].strip()), int(parts[1].strip())
    n = int(parts[0].strip())
    return n, n
