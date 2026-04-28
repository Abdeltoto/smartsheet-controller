# Anciennement « hors périmètre » — statut et orientations

Ce document remplace la section 11 du plan de simplification : on **attaque** ces sujets de façon réaliste (sécurité, effort, maintenance).

---

## 1. OAuth Smartsheet (flux supporté)

**Problème initial** : OAuth exige un **client secret** pour l’échange `code → token`. Le secret ne doit **jamais** vivre dans l’extension.

**Solution livrée**

- **Backend** : `backend/oauth_smartsheet.py` + routes montées dans `app.py`
  - `GET /api/oauth/smartsheet/config` — `client_id`, URL d’autorisation, scopes (pas de secret).
  - `POST /api/oauth/smartsheet/exchange` — échange serveur↔Smartsheet avec `SMARTSHEET_OAUTH_CLIENT_ID` / `SMARTSHEET_OAUTH_CLIENT_SECRET`.
- **Extension** — permission **`identity`**, section Options **Sign in with Smartsheet** (`oauth-options.js`)
  - `chrome.identity.launchWebAuthFlow` + redirection `chrome.identity.getRedirectURL()` (à enregistrer dans **Smartsheet Developer Tools** pour l’app OAuth).
  - Le token est affiché / copiable vers le Controller (collage manuel dans le formulaire existant pour l’instant).

**Configuration** : variables dans `.env.example` — voir commentaires.

---

## 2. « Remplacer le backend par des appels API depuis le background » — non retenu tel quel

**Risque** : exposer jetons Smartsheet + clés LLM dans le **service worker**, duplication des tools, pas d’audit / rate-limit unifiés.

**À la place**

- Le **background** ne fait que **contexte d’URL** + badge.
- La logique métier reste sur **ton FastAPI**.

---

## 3. Duplication de la bibliothèque de prompts (évitée)

**Solution** : page **`prompts-browser.html`** qui appelle **`GET {Controller}/api/prompts`** — une seule source de vérité, pas de JSON dupliqué dans l’extension.

---

## Synthèse

| Ancien hors périmètre | Décision |
|------------------------|----------|
| OAuth | Implémenté **serveur + extension** (`identity`, options). |
| API background | **Refusé** pour le produit complet. |
| Prompts dupliqués | **Évité** ; fetch live `/api/prompts`. |
