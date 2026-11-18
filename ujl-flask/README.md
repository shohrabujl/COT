# UJL Plan Control Center (runs on your PC)

The plan workbook as a web app: sign-in, Super User approval, role permissions, audit log, dashboard, Kanban board, plan views, data health, line-wise tracking and the PF master. All rules live in `ujl/engine.py`.

## Start
1. Install Python 3.9 or newer once from python.org. Tick **Add python.exe to PATH**.
2. Double-click **start.bat**. The first start installs Flask and openpyxl (needs internet once). The browser opens at http://127.0.0.1:5000.
3. First screen: create the **Super User**. If `UPDATE_PLAN_MASTER_ANALYSIS.xlsx` is in this folder it loads by itself (plan, PF master, saved cards, line-wise rows). If not, open **Health** and import it.

Close the black window to stop the app. Mac or Linux: run `./start.sh`.

## Everyday use
- **Monthly plan:** Health → choose the workbook → Read file → tick what to load → Commit. Plan and PF master are replaced; saved cards and line-wise rows stay unless you tick them.
- **New people:** they choose *Request access* on the sign-in page. A Super User approves them in Admin → Users and picks a role.
- **Roles:** Super User, Planner, Line Supervisor, Viewer. Edit them in Admin → Permissions (Super User only), or add your own roles.
- **Practice first:** `python tests/sample.py practice.xlsx` makes a small workbook you can import to try everything.

## Your data
- `data/ujl.db` holds everything, `data/secret.key` signs sessions. To back up, close the app and copy the `data` folder.
- Keep this folder on the PC's own disk, not inside OneDrive or Google Drive sync.
- To update the program, replace everything except the `data` folder.
- Forgot the only Super User password: `.venv\Scripts\python manage.py reset-password USERNAME`

## What protects it
- Only this PC can open it (127.0.0.1), and it refuses other host names.
- Passwords are stored hashed. Five wrong passwords lock that username for 15 minutes. Every form is protected against forged requests.
- Permissions are checked on the server for every action, not just hidden in the page. Only a Super User can change Super Users or the permission table, so nobody can give themselves more.
- The audit log is tamper-evident: each entry’s fingerprint covers the one before it, and **Verify chain** finds edited or deleted lines. It cannot stop someone who rewrites the whole database file, and anyone who can copy files on this PC can read `data/ujl.db`. Do not publish this app on a network without extra hardening (HTTPS, a real web server).

## Checks
- `.venv\Scripts\python -m unittest discover -s tests` runs 50 tests on a temporary database.
- `python tests/parity.py UPDATE_PLAN_MASTER_ANALYSIS.xlsx` compares the engine with the workbook (59 checks).
