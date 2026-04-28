# Captures pour le Chrome Web Store

## Formats attendus

- Vérifier dans le tableau de bord Chrome Web Store la liste exacte (elle évolue) ; en général :
  - **Screenshots principales** : **1280 × 800** px *(ou)* **640 × 400** px · PNG ou JPEG · au moins **1**.
  - Optionnel : **promo petite** ≈ **440 × 280** px selon invite.

---

## Séquence conseillée (3 à 5 images)

| # | Sujet | Comment faire |
|---|--------|----------------|
| 1 | **Vue d’ensemble** — onglet `app.smartsheet.com/sheets/...` avec le panneau latéral ouvert et l’iframe Controller (formulaire ou chat). | Fenêtre élargie ; side panel bien visible à droite. |
| 2 | **Bandeau** — pastille bleue + texte `Sheet · …` lorsqu’une feuille Smartsheet est ouverte | Zoom léger ou recadrage sur la barre haute si la capture 1 est trop dense. |
| 3 | **Options** — `options.html` avec l’origine locale ou prod | Maximiser uniquement cet onglet ; fond sombre cohérent. |
| 4 | *(Optionnel)* Landing Controller en mode embed dans le panel (liste marketing masquée). | Confirme le mode `ssc_ext`. |
| 5 | *(Optionnel)* Icône outil barrée puzzle + tooltip du nom de l’extension | Lisibilité logo 32/48 px. |

---

## Outils rapides

- **Windows** : `Win + Shift + S` (outil Capture d’écran) → recadrage → enregistrer en PNG ; redimensionner si besoin (Paint, GIMP, Photoshop).
- Respecter une ** même résolution cible ** pour tout le jeu (par ex. tout en 1280×800 avec bandes si nécessaire).

---

## À ne pas faire

- Pas de données personnelles lisibles (e-mails workspace, jetons dans les champs) — utilise des **valeurs fictives** ou masque après capture.
- Pas de fenêtre coupée où on ne voit pas l’extension.

Les fichiers dans ce dossier peuvent être **gitignorés** si tu préfères ne pas committer les PNG (ajouter une règle au `.gitignore` du repo si besoin).
