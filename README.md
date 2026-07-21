# Kira — firm console (Phase-0+)

Turns **anything a bookkeeper receives** — messy Excel books (any layout,
multi-sheet, English/Malay/Chinese headers), CSVs, PDF invoices, receipt
photos — into coded, validated, reviewed **Purchase Invoices posted into SQL
Accounting** via the free official SDK. Built for firms running many client
books side by side.

Pipeline: `drop files → parse/extract → classify (rules → Claude → fallback)
→ validate (dups, tax math, dates, master codes) → review → learn → post`

What sets it apart from OCR-to-CSV tools:
- **Excel-first**: reads the bookkeeper's own book, however messy; AI maps
  unknown layouts when heuristics fail
- **Validation gate**: duplicate detection (in-batch AND against everything
  ever posted), tax-rate math, date sanity, codes checked against the client's
  real masters — errors block posting
- **Per-client learning**: majority-vote rules from every approval; audit
  trail logs each correction (auto-accuracy metric on the dashboard)
- **Posted, not exported**: entries land inside SQL via the SDK, grouped into
  invoices, with a double-posting guard

## Architecture (product)

```
client WhatsApps a receipt ─┐
bookkeeper uploads Excel ───┼─> KIRA CLOUD (server.py)        KIRA AGENT (agent.py)
supplier emails invoice ────┘   parse · AI code · validate     on the PC where SQL runs
                                review/approve · queue    ──>  polls outbound, posts via
                                firm dashboard · audit         the free SDK, reports back
                                                                     │
                                                               SQL Accounting ✓
```

Batch lifecycle: `review → approved → dispatched → posted | failed` — every
transition is recorded; nothing is lost between cloud and SQL.

### Deploy

See [DEPLOY.md](DEPLOY.md): push to GitHub (`scripts\publish_github.ps1`),
then Render → New → Blueprint → Apply (`render.yaml` does the rest). Run the
console anywhere against the deployed cloud with:

```powershell
$env:KIRA_API_URL = "https://kira-cloud.onrender.com"
$env:KIRA_FIRM_TOKEN = "<token from Render>"
streamlit run app.py
```

### Run the product stack

```powershell
# 1. Kira Cloud (anywhere)
uvicorn server:app --port 8600

# 2. Kira Agent (on the PC with SQL Accounting; edit agent_config.yaml first)
python agent.py            # or --once for a single poll

# 3. Console UI (mode: cloud in config.yaml queues batches for the Agent)
streamlit run app.py

# 4. Telegram intake (optional): create a bot via @BotFather, then
$env:TELEGRAM_BOT_TOKEN="123:abc"; python telegram_bot.py
```

Console tabs: **Convert** (drop + review + approve) · **Inbox** (verify
batches that arrived by Telegram/WhatsApp/API — nothing posts without human
approval here) · **Connections** (Agent heartbeats; one client = one .FDB
company file, one Agent serves many) · **Firm overview** · **Client history**.

Intake safety: identical files are refused at the door (per-client content-hash
log); re-exported files with the same rows are caught line-by-line by
DUP_POSTED; unsupported types are skipped with an explanation, never silently.

API quick reference (Authorization: Bearer <token> from config.yaml):

| Endpoint | Token | Purpose |
|---|---|---|
| `POST /api/clients/{c}/upload` | firm | multipart files -> coded batch in review |
| `POST /api/intake/whatsapp?phone=` | none (webhook) | maps sender via client_data/phone_map.yaml |
| `GET/POST /api/batches[...]/approve` | firm | review queue; approve refuses dirty batches (409) |
| `POST /api/agent/poll` / `report` | agent | outbound Agent protocol |
| `GET /api/firm/overview` | firm | clients + pipeline queue |

Tests: `python scripts/test_cloud_flow.py` covers upload → approve → agent
poll → post → report → dedup-on-reupload → WhatsApp intake → auth.

## One-command conversion (no UI)

```powershell
# parse + code + validate, write a review CSV for the bookkeeper
python convert.py inbox\june.xlsx --client DEMO_CLIENT

# post immediately IF fully clean (zero errors, zero low-confidence lines)
python convert.py inbox\june.xlsx --client DEMO_CLIENT --post

# post a review CSV after editing/approving it in Excel
python convert.py --from-review review_DEMO_CLIENT_20260721.csv --client DEMO_CLIENT --post
```

Exit codes: 0 = done, 1 = needs human review, 2 = posting failure.
Re-posting the same batch is refused (`DUP_POSTED` on every line).

### Conversion integrity guarantees

- Every sheet's parsed sum is reconciled against the book's **own TOTAL rows** —
  a mismatch is reported, so a silently missed row is impossible to overlook
- Repeated header rows, subtotal/balance rows, and note sheets are skipped
  *and reported*, never silently swallowed
- Excel serial dates, `RM 1,234.56`, `(50.00)` negatives, kredit columns
  (credit notes), and merged-cell supplier names are handled
- Validation blocks posting on duplicates (in-batch and vs. all history),
  unknown codes, impossible tax, and future dates
- Every posted batch carries a control total to tick against SQL

## Quick start

```powershell
pip install -r requirements.txt
python scripts/make_sample_data.py     # synthetic demo data (skip if using real data)
streamlit run app.py
```

Then drop an Excel into the uploader, review the coded lines, fix the flagged
ones, click **Approve & post batch**. In dry-run mode the batch is written to
`posted/batch_*.json` instead of SQL.

## Using real client data

1. Export the client's masters into `client_data/<CLIENT>/`:
   - `chart_of_accounts.csv` — columns `code, description, type`
   - `suppliers.csv` — columns `code, name`
   - `tax_codes.csv` — columns `code, description, rate`
2. Point `config.yaml → client.data_dir` at that folder.
3. Set `ANTHROPIC_API_KEY` so classification uses Claude (without it, a weak
   offline fallback runs and everything is flagged for review).

## Posting to SQL for real (on the firm's machine)

1. SQL Accounting must be installed on that machine, logged into a **BACKUP
   copy** of the client company. The SDK ("SDK Live", COM class `SQLAcc.BizApp`)
   is free — see wiki.sql.com.my/wiki/SDK_Live.
2. First run the field spike to confirm this SQL version's detail field names:
   ```python
   from kira.poster import SQLConfig, dump_fields
   print(dump_fields(SQLConfig(dry_run=False, user="ADMIN", password="...",
         dcf_path=r"C:\eStream\SQLAccounting\Share\Default.DCF",
         fdb_name="ACC-0001.FDB")))
   ```
   Adjust `kira/poster.py` if the detail account field differs.
3. Set `sql.dry_run: false` in `config.yaml` and fill in the login details.
4. Post a small batch, open SQL Accounting, and verify the invoices + creditor
   balances by hand before trusting anything.

## Phase-0 success criteria (from the plan)

- ≥85% of lines coded correctly first pass on a real 100–200 line batch
- Posted batch reconciles (totals tie, control accounts move correctly)
- Bookkeeper review time ≤25% of manual entry
- Second batch shows the flywheel: corrections from batch 1 auto-code batch 2

## Layout

```
kira/ingest.py     tolerant Excel/CSV parser (multi-sheet, MY/EN/CN columns,
                   junk rows, AI layout fallback for unknown formats)
kira/documents.py  PDF/photo invoice extraction via Claude vision (incl. TIN
                   capture for e-Invoice readiness)
kira/context.py    client masters (COA, suppliers, tax codes) from CSV exports
kira/classify.py   rules -> Claude (grounded in client masters) -> offline fallback
kira/rules.py      per-client majority-vote learned rules (the flywheel)
kira/validate.py   pre-posting validation & reconciliation engine
kira/audit.py      append-only audit trail + corrections log per client
kira/registry.py   multi-client registry + firm overview
kira/poster.py     SDK poster (dry-run / live COM), double-posting guard,
                   dump_fields() spike helper
kira/ui.py         brand identity — CSS, hero landing, ledger dropzone
.streamlit/        native brand theme (ledger green on cool paper)
app.py             Streamlit firm console (process / dashboard / history tabs)
scripts/           sample data generator + end-to-end pipeline tests
```

## Adding a client

Create `client_data/<CLIENT_NAME>/` with the three master CSVs — the client
appears in the sidebar automatically. Rules, audit trail, and the posted-doc
registry are kept per client in the same folder.

Tests: `python scripts/test_pipeline.py`
