# Phase D — Soumission Chrome Web Store (pas à pas)

## 1. Compte développeur (paiement unique)

1. Va sur **[Chrome Web Store Developer Dashboard](https://chrome.google.com/webstore/developer/dashboard)**.
2. Accepte les règles et paie les **frais d’enregistrement développeur** (montant fixé par Google ; vérifier la page officielle).
3. Compte Google recommandé : celui que tu utiliseras aussi pour le support utilisateurs.

---

## 2. URL de la politique de confidentialité (HTTPS)

Le formulaire Store exige une **URL publique en HTTPS**.

**Option A — GitHub Pages**

1. Dans le dépôt, place une copie de `privacy-policy.html` lisible depuis le web, par ex. branche **`gh-pages`** ou dossier **`docs/`** avec Pages activé (Settings → Pages → branch / dossier source).
2. Exemple de chemin stable :  
   `https://<username>.github.io/<repo>/extension/store/privacy-policy.html`  
   (Ajuster selon ta structure ; tester l’URL en navigation privée.)

**Option B — Ton site / VPS**

Upload `privacy-policy.html` sous un domaine HTTPS que tu contrôles.

**Option C — Petit hébergeur gratuit**

Netlify Drop, Cloudflare Pages, etc. — un seau statique avec le fichier HTML suffit.

> Copie cette URL dans le champ **Privacy policy** du listing et vérifie qu’elle s’affiche sans login.

---

## 3. Paquet ZIP à uploader

Le Store attend un **.zip du dossier extension** (celui avec `manifest.json` à la racine du ZIP), **sans** le reste du monorepo.

Contenu minimal :

```
manifest.json
background.js
sidepanel.html
sidepanel.js
options.html
options.js
icons/
content/
styles/
scripts/   # optionnel (génération icons) — peut être exclu pour réduire la taille
```

**Pour alléger l’artifact** tu peux omettre `scripts/` (les PNG dans `icons/` sont requis).

**PowerShell (depuis la racine du repo)** :

```powershell
cd extension
Compress-Archive -Path manifest.json,background.js,sidepanel.html,sidepanel.js,options.html,options.js,oauth-options.js,prompts-browser.html,prompts-browser.js,icons,content,styles -DestinationPath ..\smartsheet-controller-extension.zip -Force
```

Ou zip manuel : sélectionne les fichiers/dossiers listés → envoyer vers un fichier compressé → renommer en `.zip`.

Tester en local :

1. `chrome://extensions` → Charger l’extrait depuis le dossier ou décompresser le ZIP dans un dossier test et charger ce dossier (même contenu manifest).

---

## 4. Captures d’écran (voir `screenshots/README.md`)

- Au moins **1** capture ; jusqu’à **5** conseillées.
- Formats courants acceptés : **1280×800** ou **640×400** (PNG ou JPEG).

Contenu conseillé :

1. Vue **Smartsheet** + panneau latéral avec le Controller visible.
2. Page **Options** (URL serveur).
3. (Optionnel) bandeau “sheet détecté” avec pastille bleue.

Nommer les fichiers clairement : `screenshot-sidepanel-1280.png`, etc.

---

## 5. Petite vignette promo (facultatif mais utile)

- **Taille souvent utilisée : 440×280** (vérifier l’invite exacte dans le tableau de bord au moment du dépôt).
- Une seule image mettant en avant le side panel ou le logo + texte court.

Tu peux la produire depuis Figma ou un export du navigateur redimensionné (sans flou trop fort).

---

## 6. Remplir le tableau de bord

1. **New item** → upload du ZIP.
2. Renseigner titre, descriptions (voir `chrome-web-store-listing.md`).
3. Privacy policy URL ; section **Privacy practices**.
4. Régler la visibilité : **Public** ou **Unlisted** pour tester avant large diffusion.
5. Soumettre pour **révision**.

Délais typiques : de quelques heures à quelques jours selon la charge des reviewers.

---

## 7. Après publication

- Mettre à jour la bannière dans l’app web principale avec le **lien Chrome Web Store** réel au lieu du lien GitHub brut (facultatif).
- Incrémenter `version` dans `manifest.json` à chaque nouvelle publication :  
  **`major.minor.patch`** doit augmenter dans le tableau de bord pour chaque envoi réussi.
