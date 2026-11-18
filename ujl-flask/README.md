# UJL Plan Control Center

The plan workbook as a web app: sign-in, Super User approval, role permissions, audit log, dashboard, Kanban board, plan views, data health, line-wise tracking and the PF master.

## Run locally
1. Install Python 3.9 or newer.
2. Double-click `start.bat` (Windows) or run `./start.sh` (macOS/Linux).
3. On first run, create the Super User. The app uses SQLite under `data/` by default.

## Deploy on Vercel with PostgreSQL
This project supports Vercel Functions with a persistent PostgreSQL database. **Do not deploy with local SQLite as the production database**: Vercel's function filesystem is ephemeral.

1. Create a PostgreSQL database with a provider such as Neon. Copy its pooled or direct connection URL; it must be a PostgreSQL URL (`postgresql://...`).
2. In Vercel, import this GitHub repository and set **Root Directory** to `ujl-flask`.
3. Add these environment variables to the Vercel project (Production and Preview as appropriate):
   - `DATABASE_URL`: the PostgreSQL connection URL.
   - `SECRET_KEY`: a long random secret (at least 32 characters). Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`.
   - `APP_ALLOWED_HOSTS`: optional comma-separated custom hostnames if using a custom domain.
4. Deploy. The database schema and default roles are initialized on startup. Visit the deployed site and create the first Super User.
5. Upload the Excel workbook from the app's Health page. The local PC workbook and `data/ujl.db` are not deployed or migrated automatically.

The Vercel deployment accepts its current deployment hostname and production hostname, plus any names in `APP_ALLOWED_HOSTS`. Set the environment variables for Preview too if you want preview deployments to use the database; consider a separate preview database.

### Important data and security notes
- The first account created on the deployed instance becomes the Super User; secure the URL and do this promptly.
- Use a separate production database and secret from development/preview environments.
- Back up PostgreSQL regularly. Existing local SQLite data must be migrated separately; don't delete or overwrite your local `data/` folder.
- Vercel functions are serverless and may handle requests concurrently. PostgreSQL is used for durable shared state and a transaction-scoped lock protects the audit chain.

## Checks
Run `python -m unittest discover -s tests -v` from this directory. Tests use temporary local SQLite databases.
