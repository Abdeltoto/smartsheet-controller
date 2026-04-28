# Plan de simplification — Extension Chrome (référence d’exécution)

Document de travail pour une extension **professionnelle, minimaliste**, alignée sur les attentes “store quality”.  
**Périmètre** : tout ce qui suit s’exécute dans le dossier `extension/` sauf mention contraire.

---

## 1. Vision produit

**Rôle de l’extension** : être un **cadre** (contexte Smartsheet + accès serveur) autour de l’app web existante — **pas** un second produit à part entière.

| Principe | Signification concrète |
|----------|-------------------------|
| Une action claire | Le panneau répond à : *« Parler à ma feuille Smartsheet ouverte sans quitter le navigateur »*. |
| Minimum de chrome | Le bandeau ne répète pas la doc de l’app (token, prompts, etc.). |
| Essentiel hors ligne de flottaison | Détails dans options, tooltips, ou la page web plein écran. |
| Cohérence visuelle | Palette et typographie alignées sur l’app (déjà `#060b18`, Inter) — pas de thème “gadget”. |

---

## 2. Ce qui reste dans le dépôt racine (hors `extension/`)

- **Aucune dépendance fonctionnelle** : l’app doit continuer à fonctionner sans extension.
- **Exception acceptée** : **une mention légère sur le site** (landing) pointant vers le dossier `extension/` / instructions d’installation — “publicité” informative, pas de bannière intrusive (voir implémentation séparée dans le frontend).

---

## 3. Architecture cible (uniquement fichiers sous `extension/`)

```
extension/
├── manifest.json          # MV3, permissions strictes au besoin
├── background.js            # Détection URL, badge, aucune logique lourde
├── sidepanel.html         # Shell minimal
├── sidepanel.js           # URL iframe + état du bandeau
├── options.html / options.js
├── styles/                # (optionnel) CSS dédié side panel + options
├── content/               # (phase 2 — voir §6)
│   └── embed.css          # Stylage “mode intégré” si content script
└── icons/                 # 16, 32, 48, 128 — prêt Web Store
```

---

## 4. Bandeau latéral (side panel) — état “pro”

**Contenu maximal** (une seule ligne visible) :

1. **Indicateur** (pastille) : contexte feuille OK / non.
2. **Texte court** : `Feuille · ID raccourci` **ou** phrase unique si pas d’ID.
3. **Contrôle unique** : bouton **⚙** → page Options (URL du Controller).

**À ne pas remettre** dans une V1 pro :

- Paragraphes “collez votre token…”
- Liens multiples
- Logs ou IDs techniques en monospace pleine largeur sans troncature

**Détails** : ID complet en `title` (tooltip) ou copie depuis l’app.

**Décision UX** : le bandeau fait **≤ 36 px de hauteur** pour maximiser l’iframe.

---

## 5. Page Options — minimalisme

- **Un seul champ critique** : origine du serveur (`http(s)://hôte:port`).
- Texte d’aide **une ligne** : *« Même URL que lorsque vous ouvrez Controller dans un onglet. »*
- Bouton Enregistrer + confirmation courte (toast ou texte vert 2 s).
- **Pas** de tutoriel Smartsheet ici → renvoyer vers le site/app.

**Futur optionnel** : case “Ouvrir le panneau au clic sur l’icône uniquement” (si API le permet sans complexité).

---

## 6. Expérience “embed” sans modifier `frontend/index.html` (phase 2)

Objectif : quand la même UI est dans l’iframe, **réduire le bruit** (landing marketing, pied de page…) **sans** toucher au repo principal.

**Mécanisme** :

- Déclarer un **`content_scripts`** dans `manifest.json` avec `matches` sur les origines du Controller (localhost + domaine prod ajoutés en `host_permissions`).
- Dans le script : si `window.parent !== window` (iframe) ou paramètre réservé `?ssc_ext=1` injecté uniquement par l’extension dans l’URL de l’iframe :
  - injecter une feuille `embed.css` qui masque/agrandit selectivement des blocs (landing hero, sections secondaires…) en ciblant les **classes/id stables** du HTML existant.

**Risque** : si le HTML principal change souvent, le CSS peut casser — à documenter et à garder minimal (cibler 2–3 sélecteurs larges au plus).

**Alternative plus sûre** : uniquement masquer ce qui est **sûr** visuellement (ex. mega-footer) après inspection.

> Toute cette phase reste dans `extension/` (manifest + fichiers injectés).

---

## 7. Service worker (`background.js`)

- **Responsabilités** : parser l’URL Smartsheet (`/sheets/{id}`), persister dans `chrome.storage.session`, mettre à jour le **badge** de l’action.
- **Interdits** : pas de fetch réseau lourd, pas de timers agressifs.
- **Réveil** : s’appuyer sur `tabs.onUpdated` ; éviter `setInterval`.

---

## 8. Manifest & confiance utilisateur

- `permissions` : **liste minimale** — `storage`, `sidePanel`, `tabs` ; justifier chaque nouvelle permission dans le README store.
- `host_permissions` : uniquement Smartsheet + origine(s) du Controller — **pas** de `https://*/*` en prod.
- **Privacy** : court paragraphe dans `extension/README.md` + futur `privacy.md` pour le store : *aucune donnée Smartsheet n’est envoyée aux serveurs de l’extension ; tout transite vers votre backend.*)

---

## 9. Identité visuelle & packaging

| Élément | Action |
|---------|--------|
| Icônes | Fournir 16 / 32 / 48 / 128 px (déclin du logo Smartsheet Controller existant ou picto abstrait grille + bulle). |
| Nom | “Smartsheet Controller” cohérent avec l’app. |
| Description courte | 1 phrase orientée bénéfice : *Side panel + détection de feuille.* |
| Thème options | Même fond sombre que le side panel pour cohérence. |

---

## 10. Phases d’exécution (checklist)

### Phase A — Fondations (déjà partiellement fait)

- [x] Bandeau une ligne + pastille + ⚙
- [x] Options : URL serveur
- [x] Hauteur bandeau plafonnée (36 px), copy UI extension en **anglais** (cohérence produit global)

### Phase B — Finition “pro”

- [x] Jeu d’icônes outillés + déclaration dans `manifest`
- [x] Options : alignement visuel avec side panel (CSS dédié `styles/options.css`)
- [x] Texte `options` + README orientés “utilisateur final”

### Phase C — Embed sans toucher au frontend (optionnel)

- [x] `?ssc_ext=1` ajouté par `sidepanel.js` à l’URL iframe
- [x] Content script + `content/embed.css` (masquage landing marketing en iframe)

### Phase D — Chrome Web Store (quand prêt)

- [x] Captures — guide dimensions + liste des plans (`extension/store/screenshots/README.md`)
- [x] Politique de confidentialité — fichier **`extension/store/privacy-policy.html`** + guide d’hébergement HTTPS (`SUBMISSION-GUIDE.md`)
- [x] Fiche Store — textes & justifications (`chrome-web-store-listing.md`), ZIP (`SUBMISSION-GUIDE.md`)
- [ ] Déposer soi‑même : compte payant développeur, ZIP, screenshots finales, soumission dashboard — **manuel**

---

## 11. Hors périmètre (historique)

Les sujets volontairement exclus du MVP (OAuth lourd, API en background, duplication des prompts) ont été **traités ou tranchés** — voir **`extension/OUT-OF-SCOPE-ROADMAP.md`**.
---

## 12. Récapitulatif une phrase

L’extension **professionnelle** = **peu de chrome**, **permissions justifiées**, **un seul message par zone**, **tout le flux métier** dans l’iframe vers **votre** serveur — avec option plus tard d’un **mode visuel allégé** via content script **sans modifier** le dépôt principal.
