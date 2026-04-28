# Smartsheet Controller — Extension Chrome

Loads the Controller web app (your FastAPI server) in the **Chrome side panel** next to Smartsheet:

- Reads the sheet ID from `app.smartsheet.com/.../sheets/<id>` and passes `sheet_id` + **`ssc_ext=1`** to strip marketing chrome in the iframe (content script — no changes to the main repo).
- Puts a blue dot on the toolbar icon when a sheet URL was detected.

**Product plan & checklist:** **[`SIMPLIFICATION-PLAN.md`](SIMPLIFICATION-PLAN.md)**

**Chrome Web Store (Phase D)** — privacy policy HTML, listing copy, ZIP, screenshots checklist: **[`store/`](store/)**.

## Optional features (formerly “out of scope”)

- **OAuth Smartsheet** — Set `SMARTSHEET_OAUTH_CLIENT_ID` and `SMARTSHEET_OAUTH_CLIENT_SECRET` on the server (see repo `.env.example`). In **Extension Options**, register redirect `https://<extension-id>.chromiumapp.org/` in Smartsheet Developer Tools, then use **Sign in with Smartsheet**. Copy the token into the Controller connect form. Architecture notes: **[`OUT-OF-SCOPE-ROADMAP.md`](OUT-OF-SCOPE-ROADMAP.md)**.
- **Prompt catalogue** — **`prompts-browser.html`** loads **`/api/prompts`** from your running server (no duplicate JSON). Open from Options: *Open prompts in a new tab*.

## Prerequisites

1. Backend running: `uvicorn backend.app:app --reload --port 8100` (OAuth endpoints load with the app).
2. Chrome → `chrome://extensions` → Developer mode → **Load unpacked** → choose this **`extension`** folder.

## Usage

1. Open a sheet on [Smartsheet](https://app.smartsheet.com) (optional — auto-fills sheet ID).
2. Click the extension icon (or **Ctrl+Shift+Y**) to open the side panel.
3. Sign in inside the frame (API token → Find sheet → Connect). UI copy in the narrow bar is English; the web app stays as-is.

**Server URL:** ⚙ on the thin bar above the iframe, or right‑click the extension → Options. Add your HTTPS origin under **`host_permissions`** + **`content_scripts`** `matches` + **`web_accessible_resources`** `matches` if you deploy remotely.

## Icon assets

PNG icons live in **`icons/`**. Regenerate after editing **`scripts/generate_icons.py`**:

```bash
cd extension && python scripts/generate_icons.py
```

Requires **Pillow** (`pip install pillow`).

## Embed mode (`ssc_ext`)

The iframe loads `…/?ssc_ext=1&sheet_id=…`. A content script injects **`content/embed.css`** only when embedded (iframe or explicit query param) so the landing scrolls straight to **Connect** — no duplicate help text in the extension bar.

## Privacy

The extension does not send your tokens to us. Traffic is between your browser and **your** Controller server (see note on the Options page).

## File map

| Path | Role |
|------|------|
| `manifest.json` | MV3, icons, side panel, options, content scripts |
| `background.js` | Smartsheet URL → `session` storage + badge |
| `sidepanel.html`, `sidepanel.js` | 36px bar + iframe URL |
| `options.html`, `options.js`, `styles/options.css`, `oauth-options.js` | Controller URL + optional OAuth |
| `prompts-browser.html`, `prompts-browser.js`, `styles/prompts-browser.css` | Prompts via `/api/prompts` |
| `content/embed.js`, `content/embed.css` | Minimal landing in embed |
| `scripts/generate_icons.py` | Build `icons/icon-*.png` |
| `store/` | Chrome Web Store (privacy, listing, screenshots) |
| `OUT-OF-SCOPE-ROADMAP.md` | OAuth vs background API vs prompts |

## Possible next steps

- Chrome Web Store packaging (screenshots, privacy policy URL) — Phase D in the plan.
- Expand `host_permissions` / `matches` for your production domain.
