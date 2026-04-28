# Chrome Web Store — texte de fiche (copier-coller)

Référence officielle : [Program policies](https://developer.chrome.com/docs/webstore/program-policies/) · [Listing guidelines](https://developer.chrome.com/docs/webstore/best-listing/)

---

## Nom (max 75 caractères)

```
Smartsheet Controller
```

*(Si pris :)* `Smartsheet Controller — Side panel`

---

## Résumé court (132 caractères max — carte du store)

```
Side panel for Smartsheet: opens your Controller next to your sheet and pre-fills sheet ID from the tab URL. Self-hosted.
```
*(Compter les caractères ; raccourcis si besoin.)*

---

## Description détaillée (suggestion)

```
Smartsheet Controller brings your own self-hosted AI chat (FastAPI “Controller”) into Chrome’s side panel so you work next to app.smartsheet.com without juggling tabs.

WHAT IT DOES
• Detects the active sheet ID from the Smartsheet URL (/sheets/…) and passes it to your Controller with one click.
• Opens your Controller in the side panel iframe — same app you run in a normal tab.
• Optional “embed” mode keeps the connect flow visible in a narrow panel.

WHAT YOU NEED
• Your Smartsheet Controller server running (same project as the web app).
• Set the server URL in extension options (default http://127.0.0.1:8100 for local dev).
• Your existing Smartsheet API token is entered inside the Controller UI (not sent to the extension author).

PRIVACY IN SHORT
The extension does not collect your tokens or sheet data for third parties. It reads tab URLs to extract sheet IDs on Smartsheet and stores your Controller URL in Chrome storage. See the Privacy Policy URL in the listing.

OPEN SOURCE
Source and instructions: GitHub — smartsheet-controller repository, extension folder.

NOT AFFILIATED
“Smartsheet” is a trademark of Smartsheet Inc. This is a community extension, not affiliated with Smartsheet Inc.
```

Ajustez l’URL du dépôt / la version si votre fork diffère.

---

## Catégorie recommandée

- **Productivity** (ou **Developer Tools** si vous ciblez surtout des dev self-host).

---

## Langue principale du listing

- **English** (aligné avec l’UI actuelle du bandeau). Vous pouvez ajouter une traduction française plus tard dans le tableau de bord si disponible.

---

## Justifications des permissions (formulaire Privacy / Data)

À coller lorsque Chrome demande une justification **par permission** :

| Permission     | Texte proposition |
|----------------|-------------------|
| **storage**    | Saves the user-configured Controller server base URL (`chrome.storage.sync`), ephemeral sheet-ID context (`chrome.storage.session`), and optional OAuth token copy (`chrome.storage.local`). |
| **sidePanel**  | Displays the Controller web app in Chrome’s side panel next to Smartsheet. |
| **tabs**       | Reads the active tab URL when loading completes so we can extract a numeric sheet ID from Smartsheet app URLs (`/sheets/…`). |
| **identity**   | Optional “Sign in with Smartsheet”: opens Smartsheet’s OAuth page and returns the redirect URL to the extension; your server exchanges the code for tokens. |

**Host permissions** (`app.smartsheet.com`, `127.0.0.1`, `localhost`) :

- Loads the user’s Controller in an iframe and injects minimal layout CSS only on that Controller origin when embedded (`ssc_ext`). No remote servers operated by the publisher.

---

## Lien Politique de confidentialité (champ obligatoire)

Après publication de `privacy-policy.html` (voir `SUBMISSION-GUIDE.md`), utilisez une URL **https** stable, par ex. :

`https://<votre-domaine>/privacy-policy.html`

ou GitHub Pages (voir guide).

Ne soumettez pas une URL `file://` ou un lien Google Drive non public — le validateur doit pouvoir charger la page.

---

## Badge “Trader” / données collectées

- Cocher **No** pour “Do you collect user data?” si votre interprétation est : pas de collecte par *vous* comme éditeur (traitement local + serveur utilisateur). En cas de doute lors de l’audit, alignez les réponses sur `privacy-policy.html`.

---

## Keywords (si le tableau de bord propose des étiquettes)

`smartsheet`, `spreadsheet`, `AI`, `side panel`, `API`, `productivity`
