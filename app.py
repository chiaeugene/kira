"""Kira — firm console.

Run:  streamlit run app.py

Two data modes, chosen automatically:
  LOCAL  (default)          reads/writes this machine's client_data + batches;
                            config.yaml `mode` decides local-post vs agent-queue
  REMOTE (KIRA_API_URL set) talks to a deployed Kira Cloud over HTTPS —
                            run the console anywhere, e.g. against Render

Tabs: Convert · Inbox · Connections · Firm overview · Client history
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from kira import ui
from kira.envfile import load_env

load_env()  # .env present -> cloud mode; absent -> local mode

from kira.api_client import KiraAPI, remote_url
from kira.batches import (BatchStore, ensure_row_ids, records_to_df,
                          rows_to_records, source_channel)
from kira.classify import classify, _llm_available
from kira.documents import extract_documents
from kira.filelog import FileLog
from kira.ingest import parse_workbook
from kira.poster import PostedRegistry, SQLConfig, post_batch
from kira.registry import (client_dir, create_client, firm_overview,
                          list_clients, open_client, save_masters)
from kira.repairs import apply_fixes, propose_fixes
from kira.review import approve_batch, reject_batch
from kira.validate import summarize, validate_batch

st.set_page_config(page_title="Kira", page_icon="assets/favicon.svg", layout="wide")
ui.inject()

# ---- login gate (active only when KIRA_CONSOLE_PASSWORD is set, i.e. the
# hosted console; a locally-run console stays password-free) ----
_CONSOLE_PW = os.environ.get("KIRA_CONSOLE_PASSWORD")
if _CONSOLE_PW and not st.session_state.get("authed"):
    st.markdown(
        '<div style="text-align:center;margin-top:14vh">'
        '<div style="font-size:44px;font-weight:700;letter-spacing:-.03em">'
        'Kira<span style="color:#157A5B">.</span></div>'
        '<p style="color:#6E6E73">Sign in to the firm console</p></div>',
        unsafe_allow_html=True)
    _c1, _c2, _c3 = st.columns([1, 1, 1])
    with _c2:
        with st.form("login"):
            pw_try = st.text_input("Password", type="password",
                                   label_visibility="collapsed",
                                   placeholder="Password", key="login_pw")
            if st.form_submit_button("Sign in", type="primary",
                                     width="stretch"):
                if pw_try == _CONSOLE_PW:
                    st.session_state.authed = True
                    st.rerun()
                else:
                    st.error("Wrong password.")
    st.stop()

CONFIG = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8"))
LLM = CONFIG["llm"]
SQL = SQLConfig(**CONFIG["sql"])
MODE = CONFIG.get("mode", "local")

REMOTE = remote_url() is not None
api = KiraAPI.from_env() if REMOTE else None
batch_store = BatchStore() if not REMOTE else None

EXCEL_EXT = {".xlsx", ".xls", ".csv"}
DOC_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
EDITABLE = ["doc_type", "date", "supplier", "description", "amount", "tax",
            "doc_no", "supplier_code", "account_code", "tax_code"]
DOC_TYPE_OPTIONS = ["purchase", "purchase_return", "sale", "sales_return",
                    "customer_payment", "supplier_payment", "journal"]

# ---------- sidebar ----------
ui.sidebar_brand()

if REMOTE:
    try:
        health = api.health()
        client_rows = api.clients()
    except Exception as e:
        st.error(f"Cannot reach Kira Cloud at {remote_url()} — {e}")
        st.stop()
    clients = [c["name"] for c in client_rows]
    ai_on = bool(health.get("ai"))
else:
    clients = list_clients()
    ai_on = _llm_available()

if not clients:
    st.sidebar.error("No clients found.")
    st.stop()

def reset_client_workspace() -> None:
    """Clear per-client working state ONLY. Never touches the login
    (authed) or the client dropdown itself — clearing those was the cause
    of the console 'bouncing' back to the sign-in page on every client
    switch and upload."""
    doomed = [k for k in st.session_state.keys()
              if k in ("coded", "notes", "upload_result")
              or k.startswith(("editor_", "rows_", "repairs_"))]
    for k in doomed:
        del st.session_state[k]


default_ix = (clients.index(CONFIG["client"]["name"])
              if CONFIG["client"]["name"] in clients else 0)
client_name = st.sidebar.selectbox("Client", clients, index=default_ix,
                                   key="client_select")
if st.session_state.get("client") != client_name:
    reset_client_workspace()
    st.session_state.client = client_name

if REMOTE:
    csum = next(c for c in client_rows if c["name"] == client_name)
    n_sup, n_acc, n_rules = csum["suppliers"], csum["accounts"], csum["rules"]
    posting_label, posting_state = "queued for Agent", "ok"
else:
    ctx, store, audit = open_client(client_name)
    registry = PostedRegistry(client_dir(client_name))
    n_sup, n_acc, n_rules = len(ctx.suppliers), len(ctx.accounts), len(store)
    if MODE == "cloud":
        posting_label, posting_state = "queued for Agent", "ok"
    elif SQL.dry_run:
        posting_label, posting_state = "dry run", "warn"
    else:
        posting_label, posting_state = "live", "off"

ui.sidebar_status(ai_on, posting_label, posting_state, n_sup, n_acc, n_rules)
if REMOTE:
    st.sidebar.caption(f"Connected to {remote_url()}")

with st.sidebar.expander("+ Add a new client"):
    st.caption(
        "This exact name must also be typed into the Agent's config on the "
        "SQL PC — the Agent's setup wizard fetches this list, so you only "
        "type it once."
    )
    new_name = st.text_input("Client name", key="new_client_name",
                             placeholder="e.g. MAJU_JAYA")
    st.caption("Master data (optional now — you can add these later):")
    coa_up = st.file_uploader("Chart of accounts CSV", type=["csv"], key="nc_coa")
    sup_up = st.file_uploader("Suppliers CSV", type=["csv"], key="nc_sup")
    cus_up = st.file_uploader("Customers CSV", type=["csv"], key="nc_cus")
    tax_up = st.file_uploader("Tax codes CSV", type=["csv"], key="nc_tax")

    if st.button("Create client"):
        name = new_name.strip()
        if not name:
            st.error("Enter a client name.")
        else:
            uploads_map = {
                "chart_of_accounts.csv": coa_up, "suppliers.csv": sup_up,
                "customers.csv": cus_up, "tax_codes.csv": tax_up,
            }
            files = {fname: f.getvalue() for fname, f in uploads_map.items()
                     if f is not None}
            try:
                if REMOTE:
                    res = api.create_client(name)
                    if res.get("_conflict"):
                        st.error(res.get("message", "Could not create client."))
                    else:
                        if files:
                            api.upload_masters(name, files)
                        st.success(f"Client '{name}' created.")
                        st.rerun()
                else:
                    create_client(name)
                    if files:
                        save_masters(name, files)
                    st.success(f"Client '{name}' created.")
                    st.rerun()
            except (ValueError, FileExistsError) as e:
                st.error(str(e))

with st.sidebar.expander("Add masters"):
    st.caption(
        "Give an existing client its SQL master data — chart of accounts, "
        "suppliers, customers, tax codes (CSV exports from SQL Accounting). "
        "Kira codes lines against these; a client registered from the Agent "
        "starts without them."
    )
    am_name = st.selectbox("Client", clients, key="am_client_name")
    am_coa = st.file_uploader("Chart of accounts CSV", type=["csv"], key="am_coa")
    am_sup = st.file_uploader("Suppliers CSV", type=["csv"], key="am_sup")
    am_cus = st.file_uploader("Customers CSV", type=["csv"], key="am_cus")
    am_tax = st.file_uploader("Tax codes CSV", type=["csv"], key="am_tax")
    if st.button("Upload masters", key="am_btn"):
        am_files = {fname: f.getvalue() for fname, f in {
            "chart_of_accounts.csv": am_coa, "suppliers.csv": am_sup,
            "customers.csv": am_cus, "tax_codes.csv": am_tax,
        }.items() if f is not None}
        if not am_files:
            st.error("Choose at least one CSV first.")
        else:
            if REMOTE:
                api.upload_masters(am_name, am_files)
            else:
                save_masters(am_name, am_files)
            st.success(f"{len(am_files)} master file(s) saved for {am_name}. "
                       "Open the batch in the Inbox and press "
                       "'Re-code with AI'.")

with st.sidebar.expander("Remove a client"):
    st.caption("Irreversible — deletes this client's master data, learned "
              "rules, and audit trail.")
    rm_name = st.selectbox("Client to remove", clients, key="rm_client_name")
    rm_confirm = st.text_input(f"Type '{rm_name}' to confirm",
                               key="rm_client_confirm")
    if st.button("Remove client", key="rm_client_btn"):
        if rm_confirm.strip() != rm_name:
            st.error("Name doesn't match — nothing removed.")
        else:
            try:
                if REMOTE:
                    res = api.delete_client(rm_name)
                    if res.get("_conflict"):
                        st.error(res.get("message", "Could not remove client."))
                    else:
                        st.success(f"Removed '{rm_name}'.")
                        reset_client_workspace()
                        for k in ("client", "client_select"):
                            st.session_state.pop(k, None)
                        st.rerun()
                else:
                    from kira.registry import delete_client as delete_client_local
                    delete_client_local(rm_name)
                    st.success(f"Removed '{rm_name}'.")
                    reset_client_workspace()
                    for k in ("client", "client_select"):
                        st.session_state.pop(k, None)
                    st.rerun()
            except FileNotFoundError as e:
                st.error(str(e))

n_inbox = (len(api.batches(state="review")) if REMOTE
           else len(batch_store.list(state="review")))
tab_names = ["Convert", f"Inbox ({n_inbox})" if n_inbox else "Inbox",
             "Connections", "Firm overview", "Client history"]
tab_batch, tab_inbox, tab_conn, tab_dash, tab_history = st.tabs(tab_names)


# ---------- shared helpers ----------

def show_issues(issues: pd.DataFrame, counts: dict) -> None:
    if issues.empty:
        return
    with st.expander(
        f"Validation — {counts['error']} error(s), {counts['warning']} "
        f"warning(s), {counts['info']} note(s)", expanded=counts["error"] > 0,
    ):
        st.dataframe(issues, width="stretch", hide_index=True)


def review_editor(df: pd.DataFrame, key: str) -> pd.DataFrame:
    display = df.copy()
    display["status"] = display["confidence"].map(
        {"high": "auto", "medium": "likely", "low": "review"})
    cols = ["status"] + EDITABLE + ["confidence", "source", "reason"]
    cols = [c for c in cols if c in display.columns]
    return st.data_editor(
        display[cols],
        disabled=["status", "source", "reason", "confidence"],
        column_config={
            "doc_type": st.column_config.SelectboxColumn(
                "doc_type", options=DOC_TYPE_OPTIONS, required=True,
                help="Which SQL module this line posts into"),
            "supplier": st.column_config.TextColumn("party"),
            "supplier_code": st.column_config.TextColumn("party_code"),
        },
        width="stretch", num_rows="fixed", key=key,
    )


def apply_edits(df: pd.DataFrame, edited: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in EDITABLE:
        if col in edited.columns:
            out[col] = edited[col].values
    return out


def note_widget(n: str):
    (st.warning if "DUPLICATE" in n or "⚠" in n else st.caption)(n)


def show_repairs(fixes: pd.DataFrame, key: str) -> bool:
    """Render the suggested-repairs panel. Returns True if user clicked apply."""
    if fixes.empty:
        return False
    with st.expander(f"Suggested repairs — Kira can fix "
                     f"{len(fixes)} issue(s) for you", expanded=True):
        st.dataframe(fixes.drop(columns=["row_id"], errors="ignore"),
                     width="stretch", hide_index=True)
        st.caption("Applying updates the lines below (duplicates are removed). "
                   "You still review and approve — and everything is "
                   "re-checked at approval.")
        return st.button("Apply suggested repairs", key=key)
    return False


# =============================== CONVERT ===============================
with tab_batch:
    show_hero = "coded" not in st.session_state and "upload_result" not in st.session_state
    if show_hero:
        impact = (ui.impact_from_rows(api.overview()["clients"]) if REMOTE
                  else ui.compute_impact())
        ui.hero(impact)
        ui.drop_label()
    uploads = st.file_uploader(
        "Drop anything — messy Excel books, CSVs, PDF invoices, receipt photos",
        type=[e.lstrip(".") for e in (EXCEL_EXT | DOC_EXT)],
        accept_multiple_files=True,
        label_visibility="collapsed",
    )
    if show_hero:
        ui.features()

    # ---- REMOTE: server does everything; review happens in the Inbox ----
    if REMOTE and uploads and "upload_result" not in st.session_state:
        with st.spinner("Uploading — Kira Cloud is reading and coding…"):
            result = api.upload(client_name,
                                [(u.name, bytes(u.getbuffer())) for u in uploads])
        st.session_state.upload_result = result

    if REMOTE and "upload_result" in st.session_state:
        result = st.session_state.upload_result
        if result.get("_unparseable"):
            st.error(result.get("message", "Nothing parseable uploaded."))
            for n in result.get("notes", []):
                note_widget(n)
        else:
            st.success(
                f"Batch `{result['batch_id']}` received — {result['lines']} "
                f"line(s), RM {result['total_rm']:,.2f}, "
                f"{result['errors']} validation error(s). "
                "Open the Inbox tab to verify and approve.")
            for n in result.get("notes", []):
                note_widget(n)
        if st.button("Upload more"):
            st.session_state.pop("upload_result", None)
            st.rerun()

    # ---- LOCAL: parse + code here, immediate review ----
    if not REMOTE and uploads and "coded" not in st.session_state:
        filelog = FileLog(client_dir(client_name))
        frames, notes, doc_files = [], [], []
        with st.spinner("Reading documents and coding every line…"):
            for u in uploads:
                ext = Path(u.name).suffix.lower()
                data = bytes(u.getbuffer())
                prior = filelog.seen(data)
                if prior:
                    notes.append(f"{u.name}: DUPLICATE FILE — identical content "
                                 f"already received as '{prior['file']}' on "
                                 f"{prior['ts']}. Skipped.")
                    continue
                if ext in EXCEL_EXT:
                    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                        tmp.write(data)
                    part, part_notes = parse_workbook(tmp.name)
                    part["source_file"] = u.name
                    frames.append(part)
                    notes += [f"{u.name} {n}" for n in part_notes]
                    filelog.record(u.name, data, "upload")
                else:
                    doc_files.append((u.name, data))

            if doc_files:
                if ai_on:
                    docs = extract_documents(doc_files, model=LLM["model"],
                                             max_tokens=LLM["max_tokens"],
                                             client_name=client_name)
                    frames.append(docs)
                    notes.append(f"{len(doc_files)} document file(s) → "
                                 f"{len(docs)} entries")
                    for name, data in doc_files:
                        filelog.record(name, data, "upload")
                else:
                    st.warning(f"{len(doc_files)} PDF/image file(s) held — "
                               "AI extraction needs ANTHROPIC_API_KEY.")

        if frames:
            parsed = ensure_row_ids(pd.concat(frames, ignore_index=True))
            with st.spinner("Coding lines against this client's ledger…"):
                st.session_state.coded = classify(
                    parsed, ctx, store, model=LLM["model"],
                    batch_size=LLM["batch_size"], max_tokens=LLM["max_tokens"])
            st.session_state.notes = notes
            st.rerun()
        elif notes:
            for n in notes:
                st.warning(n)
        else:
            st.error("Nothing parseable was uploaded.")

    if not REMOTE and "coded" in st.session_state:
        df: pd.DataFrame = st.session_state.coded
        for n in st.session_state.get("notes", []):
            note_widget(n)

        issues = validate_batch(df, ctx, registry.keys)
        counts = summarize(issues)
        n_low = int((df["confidence"] == "low").sum())
        n_rule = int((df["source"] == "rule").sum())

        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Lines", len(df))
        c2.metric("Total (RM)", f"{df['amount'].sum():,.2f}")
        c3.metric("Auto-coded", n_rule)
        c4.metric("Needs review", n_low)
        c5.metric("Validation errors", counts["error"],
                  delta=f"{counts['warning']} warnings", delta_color="off")

        show_issues(issues, counts)
        fixes = propose_fixes(df, issues, ctx)
        if show_repairs(fixes, key="repairs_convert"):
            st.session_state.coded = apply_fixes(df, fixes)
            st.rerun()
        edited = review_editor(df, key="editor_convert")

        left, right = st.columns([1, 3])
        if counts["error"] > 0:
            left.button("Approve batch", type="primary", disabled=True,
                        help="Fix the validation errors first.")
            if right.button("Re-validate after edits"):
                st.session_state.coded = apply_edits(df, edited)
                st.rerun()
        elif left.button("Approve batch", type="primary"):
            approved = apply_edits(df, edited)
            missing = approved[(approved["supplier_code"] == "")
                               | (approved["account_code"] == "")]
            if not missing.empty:
                st.error(f"{len(missing)} line(s) still missing supplier or "
                         f"account codes (rows: {list(missing['source_row'])}).")
            else:
                src = [u.name for u in uploads] if uploads else ["console"]
                if MODE == "cloud":
                    b = batch_store.create(client_name, src, df, issues,
                                           st.session_state.get("notes", []))
                    ok, info = approve_batch(batch_store, b, approved)
                    if ok:
                        st.success(
                            f"Batch `{b['id']}` approved and queued — the Kira "
                            f"Agent will post it (RM {approved['amount'].sum():,.2f}, "
                            f"{info['corrections']} correction(s) learned).")
                    else:
                        st.error(info["message"])
                        st.dataframe(pd.DataFrame(info["issues"]),
                                     width="stretch", hide_index=True)
                else:
                    for _, r in approved.iterrows():
                        store.learn(r["supplier"], r["supplier_code"],
                                    r["account_code"], r["tax_code"],
                                    str(r.get("doc_type", "") or "purchase"))
                    store.save()
                    result = post_batch(approved, SQL, registry=registry)
                    audit.log_batch(", ".join(src), result, len(approved),
                                    float(approved["amount"].sum()), 0, counts)
                    st.success(f"{result['mode'].upper()}: {result['invoices']} "
                               f"invoice(s), {result['lines']} line(s).")

        if right.button("Start over"):
            for k in ("coded", "notes"):
                st.session_state.pop(k, None)
            st.rerun()

# ================================ INBOX ================================
with tab_inbox:
    st.subheader("Waiting for verification")
    st.caption(
        "Everything that arrives by Telegram, WhatsApp, or API upload lands "
        "here already coded and checked. Nothing reaches SQL without a person "
        "approving it on this screen."
    )
    pending = (api.batches(state="review") if REMOTE
               else batch_store.list(state="review"))
    if not pending:
        st.info("Inbox is clear — no batches waiting for review.")
    else:
        def label(b):
            lines = b["lines"] if REMOTE else len(b["rows"])
            chan = b["channel"] if REMOTE else source_channel(b)
            bid = b["batch_id"] if REMOTE else b["id"]
            return f"{bid} · {b['client']} · {chan} · {lines} lines · RM {b['total_rm']:,.2f}"

        options = {label(b): (b["batch_id"] if REMOTE else b["id"])
                   for b in pending}
        choice = st.selectbox("Batch", list(options.keys()))
        bid = options[choice]
        b = api.batch(bid) if REMOTE else batch_store.get(bid)

        chan = source_channel(b)
        st.markdown(
            f"**{b['client']}** &nbsp;·&nbsp; via **{chan.capitalize()}** "
            f"&nbsp;·&nbsp; {len(b['rows'])} lines &nbsp;·&nbsp; "
            f"RM {b['total_rm']:,.2f} &nbsp;·&nbsp; received {b['created_at']}",
        )
        for n in b.get("notes", []):
            note_widget(n)

        # A client registered by the Agent starts with EMPTY masters — the AI
        # has no chart of accounts to code against, so every line arrives with
        # a blank account code and approval would dead-end. Say so up front,
        # and offer a re-code once the masters exist (field feedback).
        if REMOTE:
            _cl = next((c for c in api.clients()
                        if c["name"] == b["client"]), None)
            _n_accounts = _cl.get("accounts", 0) if _cl else 0
        else:
            _bctx, _x, _y = open_client(b["client"])
            _n_accounts = len(_bctx.accounts)
        _blank_now = sum(1 for rec in b["rows"]
                        if not str(rec.get("account_code", "") or "").strip())
        if _n_accounts == 0:
            st.warning(
                f"**{b['client']} has no chart of accounts yet**, so Kira "
                "cannot assign account codes — approval will be blocked "
                "until every line has one. Fix: open the sidebar, use "
                "**Add masters** to upload this client's chart of accounts "
                "(plus suppliers / customers / tax codes), then press "
                "**Re-code with AI** below. Or type the codes into the "
                "account_code column by hand."
            )
        if _blank_now and st.button(
                f"Re-code with AI ({_blank_now} blank line(s))",
                key=f"rc_{bid}",
                help="Runs AI coding again on this batch using the client's "
                     "current master data — use after uploading masters."):
            with st.spinner("Re-coding this batch..."):
                if REMOTE:
                    api.recode(bid)
                else:
                    _ctx2, _rules2, _a2 = open_client(b["client"])
                    _reg2 = PostedRegistry(client_dir(b["client"]))
                    _coded2 = classify(records_to_df(b["rows"]), _ctx2, _rules2)
                    _iss2 = validate_batch(_coded2, _ctx2, _reg2.keys)
                    batch_store.update_rows(bid, _coded2, _iss2)
            st.session_state.pop(f"rows_{bid}", None)
            st.session_state.pop(f"editor_{bid}", None)
            st.rerun()

        rows_key = f"rows_{bid}"
        rows_df = st.session_state.get(rows_key)
        if rows_df is None:
            rows_df = records_to_df(b["rows"])
        issues = pd.DataFrame(b["issues"])
        counts = ({"error": int((issues["severity"] == "error").sum()),
                   "warning": int((issues["severity"] == "warning").sum()),
                   "info": int((issues["severity"] == "info").sum())}
                  if not issues.empty else {"error": 0, "warning": 0, "info": 0})
        show_issues(issues, counts)

        if REMOTE:
            fixes = pd.DataFrame(api.repairs(bid))
        else:
            bctx, _br, _ba = open_client(b["client"])
            fixes = propose_fixes(rows_df, issues, bctx)
        if show_repairs(fixes, key=f"repairs_{bid}"):
            st.session_state[rows_key] = apply_fixes(rows_df, fixes)
            st.rerun()

        edited = review_editor(rows_df, key=f"editor_{bid}")

        a, r, _sp = st.columns([1, 1, 2])
        if a.button("Approve batch", type="primary", key=f"ap_{bid}"):
            final = apply_edits(rows_df, edited)
            if REMOTE:
                res = api.approve(bid, rows_to_records(final))
                if res.get("_conflict"):
                    st.error(f"{res.get('message')} — {res.get('errors')} "
                             f"error(s), {res.get('blank_codes')} blank code(s).")
                    if res.get("blank_codes"):
                        st.info(
                            "Blank codes mean Kira had nothing to code these "
                            "lines to. Upload this client's master data in "
                            "the sidebar (**Add masters**), press **Re-code "
                            "with AI** above, then approve again — or fill "
                            "the party_code / account_code columns by hand."
                        )
                    st.dataframe(pd.DataFrame(res.get("issues", [])),
                                 width="stretch", hide_index=True)
                else:
                    st.success("Approved — queued for the Kira Agent.")
                    time.sleep(1.2)
                    st.rerun()
            else:
                ok, info = approve_batch(batch_store, b, final)
                if ok:
                    st.success(f"Approved — queued for the Kira Agent "
                               f"({info['corrections']} correction(s) learned).")
                    time.sleep(1.2)
                    st.rerun()
                else:
                    st.error(f"{info['message']} — {info['errors']} error(s), "
                             f"{info['blank_codes']} line(s) missing codes.")
                    if info.get("blank_codes"):
                        st.info(
                            "Blank codes mean Kira had nothing to code these "
                            "lines to. Upload this client's master data in "
                            "the sidebar (**Add masters**), press **Re-code "
                            "with AI** above, then approve again — or fill "
                            "the party_code / account_code columns by hand."
                        )
                    st.dataframe(pd.DataFrame(info["issues"]),
                                 width="stretch", hide_index=True)
        if r.button("Reject", key=f"rj_{bid}"):
            if REMOTE:
                api.reject(bid, "rejected in console")
            else:
                reject_batch(batch_store, b, "rejected in console")
            st.warning("Batch rejected (kept in history).")
            time.sleep(1.2)
            st.rerun()

# ============================= CONNECTIONS =============================
with tab_conn:
    st.subheader("How Kira reaches SQL Accounting")
    st.markdown(
        "There is no cable to plug in here — the connection **is the Kira "
        "Agent**, a small program installed once on the office PC where SQL "
        "Accounting runs. It calls out to Kira Cloud (no ports to open), picks "
        "up approved batches, and posts them through SQL's official free SDK.\n\n"
        "- **One client = one SQL company file** (`.FDB`).\n"
        "- **One Agent posts to many company files** on the same PC.\n"
        "- **More offices?** Install one Agent per PC — each announces which "
        "clients it serves."
    )

    if REMOTE:
        agents = api.agents()
    else:
        status_path = Path("batches") / "agents_status.json"
        agents = (json.loads(status_path.read_text(encoding="utf-8"))
                  if status_path.exists() else {})
    st.subheader("Agents")
    if not agents:
        st.info("No Agent has connected yet.")
    else:
        now = time.time()
        rows = []
        for name, a in agents.items():
            last = time.mktime(time.strptime(a["last_seen"], "%Y-%m-%dT%H:%M:%S"))
            age = now - last
            status = ("online" if age < 90 else
                      "recent" if age < 3600 else "offline")
            rows.append({
                "agent": name, "status": status,
                "last seen": a["last_seen"],
                "serves": ", ".join(a.get("clients", [])),
                "modes": ", ".join(f"{c}:{m}" for c, m in
                                   a.get("modes", {}).items()),
            })
        st.dataframe(pd.DataFrame(rows), width="stretch",
                     hide_index=True)
        agent_for_client = next(
            (n for n, a in agents.items()
             if client_name in a.get("clients", [])), None)
        if agent_for_client:
            st.success(f"**{client_name}** is served by Agent "
                       f"**{agent_for_client}** "
                       f"({agents[agent_for_client]['modes'].get(client_name, '?')}).")
        else:
            st.warning(f"No Agent currently announces {client_name} — add it "
                       "to agent_config.yaml on the office PC.")

    if not REMOTE:
        st.subheader(f"{client_name} — SQL company file")
        comp_path = client_dir(client_name) / "sql_company.yaml"
        comp = (yaml.safe_load(comp_path.read_text(encoding="utf-8"))
                if comp_path.exists() else {}) or {}
        with st.form("sql_company"):
            st.caption(
                "Reference for whoever maintains the Agent. SQL passwords stay "
                "in the Agent's own config on the office PC — never in the cloud."
            )
            company_name = st.text_input("Company name in SQL Accounting",
                                         comp.get("company_name", ""))
            fdb_name = st.text_input("Company database file (.FDB)",
                                     comp.get("fdb_name", ""))
            dcf_path = st.text_input(
                "DCF path on the office PC",
                comp.get("dcf_path",
                         r"C:\eStream\SQLAccounting\Share\Default.DCF"))
            notes = st.text_area("Notes", comp.get("notes", ""), height=68)
            if st.form_submit_button("Save"):
                comp_path.write_text(yaml.safe_dump({
                    "company_name": company_name, "fdb_name": fdb_name,
                    "dcf_path": dcf_path, "notes": notes,
                }, allow_unicode=True), encoding="utf-8")
                st.success("Saved.")

    with st.expander("Install a new Agent (one-time, on the SQL PC)"):
        st.markdown(
            "1. Copy the Kira folder to the PC where SQL Accounting is installed\n"
            "2. `pip install -r requirements.txt`\n"
            "3. Edit `agent_config.yaml` — server URL, agent token, and one "
            "entry per client company (keep `dry_run: true` until a "
            "backup-company test passes)\n"
            "4. `python agent.py` — it appears in the table above within a minute\n"
            "5. Verify a dry-run batch, then set `dry_run: false`"
        )

# ============================ FIRM OVERVIEW ============================
with tab_dash:
    st.subheader("Pipeline queue")
    if REMOTE:
        ov = api.overview()
        all_summaries = api.batches()
        qcols = st.columns(5)
        for col, s in zip(qcols, ("review", "approved", "dispatched",
                                  "posted", "failed")):
            col.metric(s.capitalize(), ov["queue"].get(s, 0))
        if all_summaries:
            st.dataframe(pd.DataFrame([{
                "batch": s["batch_id"], "client": s["client"],
                "state": s["state"], "channel": s["channel"],
                "lines": s["lines"], "RM": s["total_rm"],
            } for s in reversed(all_summaries)]),
                width="stretch", hide_index=True)
        overview_rows = pd.DataFrame(ov["clients"])
    else:
        all_batches = batch_store.list()
        if all_batches:
            qcols = st.columns(5)
            for col, s in zip(qcols, ("review", "approved", "dispatched",
                                      "posted", "failed")):
                col.metric(s.capitalize(),
                           sum(1 for b in all_batches if b["state"] == s))
            st.dataframe(pd.DataFrame([{
                "batch": b["id"], "client": b["client"], "state": b["state"],
                "channel": source_channel(b), "lines": len(b["rows"]),
                "RM": b["total_rm"], "created": b["created_at"],
                "files": ", ".join(b["source_files"]),
            } for b in reversed(all_batches)]),
                width="stretch", hide_index=True)
        else:
            st.caption("No batches yet.")
        overview_rows = pd.DataFrame(firm_overview())

    st.subheader("All clients")
    if overview_rows.empty:
        st.info("No clients yet.")
    else:
        needs_masters = overview_rows[
            (overview_rows["suppliers"] == 0) & (overview_rows["accounts"] == 0)
        ]
        if not needs_masters.empty:
            names = ", ".join(needs_masters["client"])
            st.warning(
                f"**{names}** "
                + ("was" if len(needs_masters) == 1 else "were")
                + " discovered by a Kira Agent but has no master data yet — "
                "AI coding falls back to weak fuzzy-matching until you upload "
                "its chart of accounts, suppliers, customers, and tax codes "
                "(sidebar → '+ Add a new client', re-use the same name)."
            )
        st.dataframe(
            overview_rows.rename(columns={
                "client": "Client", "suppliers": "Suppliers",
                "accounts": "Accounts", "learned_rules": "Rules",
                "batches_posted": "Batches", "lines_posted": "Lines",
                "total_rm": "Total RM", "auto_accuracy": "Auto-accuracy",
            }),
            width="stretch", hide_index=True,
        )
        st.caption(
            "Auto-accuracy = share of posted lines the bookkeeper did not have "
            "to correct. This number should climb every week — that's the flywheel."
        )

# ============================ CLIENT HISTORY ============================
with tab_history:
    st.subheader(f"{client_name} — audit trail")
    if REMOTE:
        h = api.history(client_name)
        stats = h["stats"]
        batches_log = pd.DataFrame(h["batches"])
        corrections = pd.DataFrame(h["corrections"])
        rules_rows = pd.DataFrame(h["rules"])
    else:
        log = audit.read()
        stats = audit.stats()
        batches_log = (log[log["event"] == "batch_posted"]
                       if not log.empty else pd.DataFrame())
        corrections = (log[log["event"] == "correction"]
                       if not log.empty else pd.DataFrame())
        rules_rows = pd.DataFrame([
            {"supplier": key,
             **dict(zip(("supplier_code", "account_code", "tax_code"),
                        max(entry["votes"].items(),
                            key=lambda kv: kv[1])[0].split("|"))),
             "seen": max(entry["votes"].values())}
            for key, entry in store.rules.items()
        ]) if store.rules else pd.DataFrame()

    if stats["batches"] == 0 and rules_rows.empty:
        st.info("No activity yet for this client.")
    else:
        b1, b2, b3 = st.columns(3)
        b1.metric("Batches posted", stats["batches"])
        b2.metric("Lines posted", stats["lines"])
        b3.metric("Auto-accuracy",
                  f"{stats['accuracy']:.0%}" if stats["accuracy"] is not None
                  else "—")
        st.markdown("**Batches**")
        if not batches_log.empty:
            st.dataframe(batches_log.drop(columns=["event"], errors="ignore"),
                         width="stretch", hide_index=True)
        else:
            st.caption("None yet.")
        st.markdown("**Corrections (what the AI got wrong — training signal)**")
        if not corrections.empty:
            st.dataframe(corrections.drop(columns=["event"], errors="ignore"),
                         width="stretch", hide_index=True)
        else:
            st.caption("None yet.")
        st.markdown("**Learned rules**")
        if not rules_rows.empty:
            st.dataframe(rules_rows, width="stretch", hide_index=True)
        else:
            st.caption("No rules learned yet.")
