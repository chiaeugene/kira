# Installing the Kira Agent on a firm's SQL PC

What to hand the firm (one folder, e.g. `C:\KiraAgent\`):

```
KiraAgent.exe        (built with: pyinstaller --onefile --name KiraAgent
                      --hidden-import win32com --hidden-import win32com.client
                      --hidden-import pythoncom --console agent.py)
agent_config.yaml    (filled in during onboarding — see below)
.env                 (two lines: KIRA_SERVER_URL=...  KIRA_AGENT_TOKEN=...)
```

No Python, no pip, nothing else. Double-click `KiraAgent.exe`:
a console window opens showing the startup summary (cloud URL, which client
companies it serves, dry-run or live) and then a live line for everything it
does. The same lines go to `kira_agent.log` next to the exe — the permanent
local trail. Close the window to stop the Agent.

## agent_config.yaml — the one-time company mapping

```yaml
agent_name: firmname-office-pc
server_url: https://kira-cloud.onrender.com   # (or via .env)
agent_token: <from Render>                    # (or via .env)
poll_seconds: 30

clients:
  MAJU_JAYA:                       # must match the client name in Kira Cloud
    dry_run: true                  # flip to false after the backup-file test
    user: ADMIN                    # SQL Accounting login for this company
    password: "********"
    dcf_path: 'C:\eStream\SQLAccounting\Share\Default.DCF'
    fdb_name: 'ACC-0001.FDB'       # THIS company's database file
  SECOND_CLIENT:
    dry_run: true
    user: ADMIN
    password: "********"
    dcf_path: 'C:\eStream\SQLAccounting\Share\Default.DCF'
    fdb_name: 'ACC-0002.FDB'
```

How to find the values: open SQL Accounting -> File -> Open Company. The list
it shows comes from the DCF file; each company in it is one .FDB. Copy the
DCF path and the exact .FDB filename per company.

## Go-live checklist (per company)

1. `dry_run: true` -> approve a test batch in the console -> the Agent window
   shows "BATCH POSTED ... mode dry_run" and the payload lands in `posted\`.
2. Restore a BACKUP copy of the company in SQL Accounting; point `fdb_name`
   at the backup; set `dry_run: false`; approve a small batch; open SQL
   Accounting and verify the Purchase Invoices + creditor balances by hand.
3. Point `fdb_name` back at the real company file. Done — from now on,
   approved batches appear inside SQL automatically.

## Auto-start with Windows (optional)

Task Scheduler -> Create Basic Task -> "Kira Agent" -> At log on ->
Start a program -> `C:\KiraAgent\KiraAgent.exe` -> Finish.
