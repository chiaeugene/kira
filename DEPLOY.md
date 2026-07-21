# Deploying Kira

Three moving parts. Only the first one lives in the cloud.

| Part | Where it runs | How |
|---|---|---|
| Kira Cloud (API) | Render | one-click Blueprint (below) |
| Console (UI) | your laptop / any PC | `streamlit run app.py` + 2 env vars |
| Kira Agent | the office PC with SQL Accounting | `python agent.py` |

## 1. Kira Cloud on Render (~3 minutes, one time)

1. Push this repo to GitHub (`scripts\publish_github.ps1` does it).
2. In the [Render dashboard](https://dashboard.render.com): **New → Blueprint**
   → select the GitHub repo → **Apply**. That's it — `render.yaml` defines
   everything, and Render auto-generates strong `KIRA_FIRM_TOKEN` /
   `KIRA_AGENT_TOKEN` values.
3. Open the service → **Environment** tab → copy the two token values.
   Your API URL is `https://kira-cloud.onrender.com` (or similar).
4. Sanity check: open `https://<your-url>/api/health` → `{"ok": true, ...}`.

Later, when ready for AI coding: set `ANTHROPIC_API_KEY` in the same
Environment tab. Nothing else changes.

**Free-tier honesty:** the free plan sleeps after ~15 min idle (first request
takes ~1 min to wake) and its storage **resets on every deploy** — fine for
trying it, wrong for a real firm. For real use switch the service to Starter
and uncomment the `disk:` block in `render.yaml` — the start script then keeps
all state on the persistent disk automatically.

## 2. Console anywhere (your "on the go" UI)

```powershell
$env:KIRA_API_URL   = "https://kira-cloud.onrender.com"
$env:KIRA_FIRM_TOKEN = "<firm token from Render>"
streamlit run app.py
```

Same console, but every tab now reads/writes the cloud: uploads are coded
server-side, the Inbox verifies batches from any channel, Connections shows
live Agent heartbeats. Unset the env vars and it's back to fully-local mode.

## 3. Agent on the office PC

In `agent_config.yaml` set `server_url` to the Render URL and `agent_token`
to the agent token (or set env vars `KIRA_SERVER_URL` / `KIRA_AGENT_TOKEN`),
then `python agent.py`. It appears on the Connections tab within a minute.

## Updating

`git push` → Render redeploys automatically. Console/Agent: `git pull`.

## Not yet wired (deliberately later)

- `ANTHROPIC_API_KEY` — enables AI coding, PDF/photo extraction, layout fallback
- `telegram_bot.py` — needs a BotFather token; run it anywhere (even your laptop)
- WhatsApp — needs a Meta Business account for the webhook
- Real multi-user auth + custom domain — before onboarding a real firm
