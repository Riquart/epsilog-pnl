# EPSILOG — Tableau de bord P&L

Application web qui transforme l'export mensuel du P&L d'**EPSILOG SAS** (rapport de gestion
du groupe CGM, `212-000 P&L PC Multi-Hierarchy`) en un tableau de bord : KPIs, graphiques, et un
compte de résultat détaillé avec **drill-down** par poste → comptes → écritures.

- **Réutilisable chaque mois** : on dépose le nouvel export `.xlsx` → tout se recalcule.
- **Backend** : Python 3.11, FastAPI, uvicorn, pandas, openpyxl.
- **Frontend** : page unique (HTML + JS vanilla + Chart.js via CDN) servie par FastAPI.
- **Persistance** : un snapshot JSON par période dans `DATA_DIR` (+ `index.json`, `account_labels.json`).
- **Auth** : mot de passe partagé (`APP_PASSWORD`), session par cookie signé. Pas d'accès anonyme.

---

## Installation locale

```bash
cd epsilog-pnl
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# variables d'environnement (optionnelles en local)
export APP_PASSWORD="un-mot-de-passe"      # si vide → app ouverte (dev only)
export SECRET_KEY="une-clef-aleatoire"     # signe les cookies de session
export DATA_DIR="$(pwd)/data"              # où sont stockés les snapshots

uvicorn app.main:app --reload
```

Ouvrir http://127.0.0.1:8000 → page de connexion → tableau de bord.
Cliquer **« + Importer un export »** pour déposer :

| Fichier | Rôle | Requis |
|---|---|---|
| `… - P&L- Epsilog.xlsx` | source du dashboard (KPIs, graphiques, tableau) | ✅ |
| `… - Total cost - Epsilog.xlsx` | détail GL des coûts → drill-down OPEX | optionnel |
| `… - Total cogs - Epsilog.xlsx` | détail GL des COGS | optionnel |

## Tests

```bash
python -m pytest          # 16 tests : valeurs de référence mai 2026, identités, réconciliation
```

Les tests nécessitent les `.xlsx` d'exemple dans `sample_data/` (non commités, confidentiels).
S'ils sont absents, les tests sont *skipped*.

---

## Déploiement Railway

1. Pousser le repo sur **GitHub**, puis Railway → **New Project → Deploy from GitHub**.
2. **Variables d'environnement** :
   - `APP_PASSWORD` — mot de passe d'accès.
   - `SECRET_KEY` — clef aléatoire longue (signature des cookies).
   - `DATA_DIR=/data`.
3. **Volume** : ajouter un Volume monté sur `/data` (sinon les snapshots sont perdus à chaque
   redéploiement — le filesystem Railway est éphémère).
4. **Start command** (déjà dans `Procfile` / `railway.json`) :
   `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
   Healthcheck : `/health`.
5. **Sécurité** : ne jamais committer les `.xlsx` réels ni `.env` (cf. `.gitignore`). Garder l'app
   derrière le mot de passe ; envisager une restriction par IP côté Railway.

---

## Architecture

```
app/
  main.py            FastAPI : routes API + sert le frontend, assemble les snapshots
  auth.py            gate mot de passe (APP_PASSWORD) + middleware cookie signé
  store.py           lecture/écriture snapshots + libellés dans DATA_DIR
  models.py          schémas Pydantic
  parsing/
    pnl.py           parser du P&L (repère chaque poste par son libellé, pas son n° de ligne)
    detail.py        parser des écritures GL (en-têtes « Compte NNNNNNN » + forward-fill)
    mapping.py       mapping compte → poste (préfixe le plus long)
mapping/account_to_poste.csv   table de correspondance — éditable
static/
  index.html         dashboard (KPIs, 4 graphiques, tableau, tiroir drill-down)
  login.html         page de connexion
tests/test_parsing.py
```

### Endpoints API

| Méthode | Route | Rôle |
|---|---|---|
| POST | `/api/login` | connexion (form `password`) → cookie de session |
| POST | `/api/logout` | déconnexion |
| GET | `/api/periods` | liste des périodes importées + dernière |
| GET | `/api/snapshot?period=YYYY-MM` | données d'une période (dernière par défaut) |
| POST | `/api/upload` | upload multipart `pnl` (requis), `cost`, `cogs` → snapshot |
| DELETE | `/api/snapshot?period=YYYY-MM` | supprime une période |
| GET/PUT | `/api/labels` | libellés métier des comptes (persistés côté serveur) |
| PUT | `/api/labels/{account}` | renomme un compte |
| GET | `/api/unmapped` | diagnostic : comptes non rattachés à un poste |
| GET | `/health` | healthcheck |

---

## ⚠️ Limites des données (important)

Le détail des coûts se rapproche **au centime** du P&L (Σ écritures *Total cost* = **915 247,51 €**
= Total Costs mensuel). Le drill-down par **compte** est donc fiable.

**MAIS** les postes de gestion (Personnel, Outsourcing, Contractors, ICT…) sont une
**reclassification** de la hiérarchie SAP, **pas** la classe comptable PCG brute. Exemple :
*Personnel expenses* = 514 822 €/mois alors que les comptes de classe 64 ne pèsent que ≈ 305 k€.

⇒ Le mapping `mapping/account_to_poste.csv` fourni est **provisoire** (heuristique par classe PCG),
avec un bandeau d'avertissement et l'écart affiché dans le tiroir de drill-down.
**Pour un rattachement exact**, demander à SAP l'export *compte → nœud du rapport 212-000* et
remplacer le CSV. L'endpoint `/api/unmapped` liste les comptes à compléter au fil de l'eau.

Le fichier COGS a une structure moins régulière (lignes orphelines, comptes de classe 5) :
parsing tolérant, lignes non rattachées signalées dans `/api/unmapped`.
