# Spare Parts Estimation Tool — Setup & Deployment

## What changed
The app no longer reads and writes CSV files. The four tables now live in a
**database** via SQLAlchemy. The same code runs against:

- **SQLite** (a local file) — the default, zero setup, for development on your PC.
- **Postgres** (hosted) — for the deployed version: it persists when your PC is
  off and is safe for several people editing at once.

You switch between them with a single setting, `DATABASE_URL`. No code changes.

The CSV files are now only **seed data**: on the very first run against an empty
database they are loaded in once, after which the database is the source of truth.

---

## Files
- `app.py` — Streamlit UI
- `logic.py` — calculation + validation (unchanged behaviour)
- `db.py` — database connection, schema, seeding, read/write helpers
- `requirements.txt` — dependencies
- `datasetts/` — seed data: `parts.csv`, `machines.csv`, `machine_parts.csv`, `kit_components.csv`, `stand_components.csv`

  `stand_components.csv` seeds the Stand Builder palette (which parts are feet /
  columns / pipes, their height and description). Columns: `PartNumber, Category,
  Height_mm, Description, Notes`. Stand components are self-contained — they do
  NOT need to exist in `parts.csv`. Manage them in the app's Stand Builder tab
  (which writes to the database); use the tab's "Export palette as CSV" button to
  refresh this seed file. Saved stand *configurations* are not seeded from a CSV.

---

## Run locally (SQLite, no setup)
```bash
pip install -r requirements.txt
streamlit run app.py
```
A file `spareparts.db` is created in the folder and seeded from the CSVs. Edits
you make in the app are saved to that file.

---

## Deploy to Streamlit Community Cloud

### 1. Put the project on GitHub
Create a repository and push these files to it (include the `datasetts` folder —
its CSVs seed the database on first run):
```bash
git init
git add app.py logic.py db.py requirements.txt datasetts/
git commit -m "Spare parts tool"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

### 2. Create a free Postgres database (so data persists)
A hosted Postgres is what keeps data alive when your PC is off and lets several
people edit safely. Free options: **Neon** (neon.tech) or **Supabase**
(supabase.com). Create a database and copy its **connection string**, which
looks like:
```
postgresql://USER:PASSWORD@HOST/DBNAME
```
> If you skip this step the app still runs on Streamlit Cloud using SQLite, but
> that file is wiped whenever the app restarts — so for real multi-user use,
> set up Postgres.

### 3. Deploy
1. Go to **share.streamlit.io** and sign in with GitHub.
2. **Create app** → pick your repo, branch `main`, main file `app.py`.
3. Under **Advanced settings → Secrets**, paste:
   ```toml
   DATABASE_URL = "postgresql://USER:PASSWORD@HOST/DBNAME"
   ```
4. **Deploy.** First load creates the tables and seeds them from the CSVs.

You'll get a public URL like `https://<your-app>.streamlit.app` that anyone can
open in a browser — no install, available whether or not your PC is on.

### 4. (Recommended) Require a login
The free tier is public by default. In the app's **Settings → Sharing** you can
restrict access to specific Google/email accounts so only your team gets in.

---

## Notes
- To reset the data back to the seed CSVs: empty the tables (or drop the database)
  and restart — it reseeds from `datasetts/` automatically.
- To change seed data later, edit the CSVs in `datasetts/` **and** apply the same
  change in the app/database (the CSVs only seed an empty database; they're not
  re-read after).
- Local SQLite and cloud Postgres are independent; they don't sync.