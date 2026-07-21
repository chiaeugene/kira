"""Kira brand UI — Apple-inspired: system sans, big tight headlines, generous
whitespace, near-white/near-black neutrals, one restrained green accent, a
crafted product visual, and zero emoji.

Public surface:
  inject()             page CSS
  hero(impact)         centered landing hero + product SVG
  features()           three capability statements with line icons
  drop_label()         quiet lead-in above the uploader
  sidebar_brand()      wordmark block for the sidebar
  sidebar_status(...)  status rows with CSS status dots (no emoji)
  compute_impact()     lifetime numbers for the hero
"""

from __future__ import annotations

import streamlit as st

from .registry import firm_overview

_SECONDS_PER_LINE = 40  # conservative manual-keying time saved per posted line

_FONT = ('-apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", '
         '"Segoe UI", Helvetica, Arial, sans-serif')

BRAND_CSS = f"""
<style>
:root {{
  --bg:#FBFBFD; --surface:#FFFFFF; --surface-2:#F5F5F7;
  --ink:#1D1D1F; --ink-2:#6E6E73; --ink-3:#86868B;
  --line:#D2D2D7; --line-soft:#E8E8ED;
  --accent:#157A5B; --accent-deep:#0E5A43;
  --good:#2F8F5B; --warn:#B8860B; --crit:#C0392B;
  --font:{_FONT};
}}

html, body, .stApp, [data-testid="stAppViewContainer"] {{
  background:var(--bg); font-family:var(--font);
  -webkit-font-smoothing:antialiased; color:var(--ink);
}}
[data-testid="stToolbar"], [data-testid="stDecoration"] {{ display:none; }}
[data-testid="stHeader"] {{ background:transparent; }}
.block-container {{ max-width:1120px; padding-top:1.5rem; padding-bottom:5rem; }}

/* ---------------- hero ---------------- */
.kira-hero {{ text-align:center; max-width:840px; margin:-6px auto 0; }}
[data-testid="stCustomComponentV1"] {{ margin-bottom:0; }}
.kira-eyebrow {{ font-size:15px; font-weight:600; color:var(--accent);
  margin:0 0 14px; letter-spacing:0; }}
.kira-title {{
  font-size:clamp(38px,6.4vw,74px); font-weight:700; line-height:1.05;
  letter-spacing:-.035em; color:var(--ink); margin:0; text-wrap:balance;
}}
.kira-sub {{
  font-size:clamp(18px,2.1vw,23px); font-weight:400; line-height:1.45;
  color:var(--ink-2); max-width:660px; margin:22px auto 0; text-wrap:balance;
}}
.kira-visual {{ margin:46px auto 0; max-width:1060px; }}
.kira-visual svg {{ width:100%; height:auto; display:block; }}

/* impact stats */
.kira-stats {{ display:flex; justify-content:center; flex-wrap:wrap;
  gap:0; margin:42px auto 4px; max-width:760px; }}
.kira-stats .s {{ flex:1; min-width:150px; padding:6px 26px; text-align:center; }}
.kira-stats .s + .s {{ border-left:1px solid var(--line-soft); }}
.kira-stats .s b {{ display:block; font-size:clamp(30px,3.6vw,42px);
  font-weight:600; letter-spacing:-.02em; color:var(--ink);
  font-variant-numeric:tabular-nums; }}
.kira-stats .s span {{ font-size:14px; color:var(--ink-3); }}

/* quiet lead-in above the real uploader */
.kira-droplabel {{ text-align:center; font-size:15px; color:var(--ink-3);
  margin:56px 0 12px; }}

/* ---------------- dropzone (clean, not busy) ---------------- */
[data-testid="stFileUploaderDropzone"] {{
  border:1px solid var(--line); border-radius:20px; background:var(--surface);
  padding:38px 28px; transition:border-color .18s, background .18s, box-shadow .18s;
}}
[data-testid="stFileUploaderDropzone"]:hover {{
  border-color:color-mix(in srgb,var(--accent) 55%,var(--line));
  background:color-mix(in srgb,var(--accent) 3%,var(--surface));
  box-shadow:0 8px 30px rgba(29,29,31,.06);
}}
[data-testid="stFileUploaderDropzoneInstructions"] span {{ font-weight:600; color:var(--ink); }}
[data-testid="stFileUploaderDropzone"] button {{
  border:1px solid var(--line-strong,#c7c7cc) !important; border-radius:980px !important;
  font-weight:500; color:var(--ink) !important; padding:6px 18px !important;
}}

/* ---------------- features ---------------- */
.kira-features {{ display:grid; grid-template-columns:repeat(3,1fr); gap:28px;
  margin:80px auto 8px; max-width:1000px; }}
@media (max-width:760px){{ .kira-features{{ grid-template-columns:1fr; gap:36px; }} }}
.kira-feat svg {{ width:30px; height:30px; stroke:var(--ink); stroke-width:1.6;
  fill:none; stroke-linecap:round; stroke-linejoin:round; }}
.kira-feat h3 {{ font-size:20px; font-weight:600; letter-spacing:-.01em;
  margin:16px 0 6px; color:var(--ink); }}
.kira-feat p {{ font-size:15.5px; line-height:1.5; color:var(--ink-2); margin:0;
  max-width:30ch; }}

/* ---------------- metric cards ---------------- */
[data-testid="stMetric"] {{ background:var(--surface); border:1px solid var(--line-soft);
  border-radius:16px; padding:16px 18px; }}
[data-testid="stMetricValue"] {{ font-variant-numeric:tabular-nums; color:var(--ink);
  font-weight:600; letter-spacing:-.01em; }}
[data-testid="stMetricLabel"] {{ color:var(--ink-3); }}

/* ---------------- tabs ---------------- */
[data-baseweb="tab-list"] {{ gap:6px; border-bottom:1px solid var(--line-soft); }}
[data-baseweb="tab"] {{ font-weight:500; color:var(--ink-2); }}
[data-baseweb="tab"][aria-selected="true"] {{ color:var(--ink); }}
[data-baseweb="tab-highlight"], [data-baseweb="tab-border"] ~ div {{ background:var(--accent); }}

/* ---------------- buttons (pill) ---------------- */
.stButton>button {{ border-radius:980px; font-weight:500; padding:8px 22px; }}
.stButton>button[kind="primary"] {{ background:var(--accent); border:1px solid var(--accent);
  color:#fff; }}
.stButton>button[kind="primary"]:hover {{ background:var(--accent-deep);
  border-color:var(--accent-deep); }}
.stButton>button[kind="secondary"] {{ border:1px solid var(--line);
  background:var(--surface); color:var(--ink); }}

/* ---------------- sidebar ---------------- */
[data-testid="stSidebar"] {{ background:var(--surface); border-right:1px solid var(--line-soft); }}
.kira-side-wordmark {{ font-size:24px; font-weight:700; letter-spacing:-.02em;
  color:var(--ink); margin:2px 0 2px; }}
.kira-side-wordmark .dot {{ color:var(--accent); }}
.kira-side-tag {{ font-size:12.5px; color:var(--ink-3); margin:0 0 6px; }}
.kira-status {{ font-size:13.5px; color:var(--ink-2); margin:5px 0;
  display:flex; align-items:center; gap:9px; }}
.kira-status .dot {{ width:8px; height:8px; border-radius:50%; flex:none;
  background:var(--ink-3); }}
.kira-status .dot.ok {{ background:var(--good); }}
.kira-status .dot.warn {{ background:var(--warn); }}
.kira-status .dot.off {{ background:var(--crit); }}
.kira-status b {{ color:var(--ink); font-weight:600; }}
.kira-side-meta {{ font-size:12.5px; color:var(--ink-3); margin-top:10px; line-height:1.6; }}

/* dataframes / tables */
[data-testid="stDataFrame"], [data-testid="stTable"] {{ border-radius:12px; overflow:hidden; }}

h3 {{ letter-spacing:-.015em; font-weight:600; }}
</style>
"""


def inject() -> None:
    st.markdown(BRAND_CSS, unsafe_allow_html=True)


# ----------------------------- product visual -----------------------------

def _hero_svg() -> str:
    # messy source rows (left): varying widths + one faded, slight offset
    messy = ""
    ys = [150, 188, 226, 264, 302, 340]
    label_w = [150, 120, 165, 95, 140, 110]
    amt_w = [46, 60, 38, 54, 42, 58]
    for i, y in enumerate(ys):
        op = 0.45 if i == 3 else 1.0
        dx = 6 if i % 2 else 0
        messy += (
            f'<rect x="{92+dx}" y="{y}" width="{label_w[i]}" height="10" rx="5" '
            f'fill="#D9D9DE" opacity="{op}"/>'
            f'<rect x="{372-amt_w[i]}" y="{y}" width="{amt_w[i]}" height="10" rx="5" '
            f'fill="#C9C9CF" opacity="{op}"/>'
        )
    # clean posted rows (right): aligned + green check
    clean = ""
    for i, y in enumerate([150, 188, 226, 264, 302]):
        clean += (
            f'<circle cx="716" cy="{y+5}" r="8" fill="#157A5B"/>'
            f'<path d="M712 {y+5} l3 3 l5 -6" stroke="#fff" stroke-width="1.6" '
            f'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
            f'<rect x="736" y="{y}" width="150" height="10" rx="5" fill="#C9C9CF"/>'
            f'<rect x="952" y="{y}" width="56" height="10" rx="5" fill="#1D1D1F" opacity="0.72"/>'
        )
    return f"""
<svg viewBox="0 0 1120 440" xmlns="http://www.w3.org/2000/svg" role="img"
     aria-label="A messy spreadsheet on the left becomes clean, posted SQL entries on the right, through Kira.">
  <defs>
    <filter id="ksh" x="-30%" y="-30%" width="160%" height="160%">
      <feDropShadow dx="0" dy="20" stdDeviation="26" flood-color="#1D1D1F" flood-opacity="0.10"/>
    </filter>
  </defs>

  <!-- left: messy source file -->
  <g filter="url(#ksh)" transform="rotate(-2.2 250 240)">
    <rect x="64" y="96" width="336" height="300" rx="20" fill="#FFFFFF" stroke="#E8E8ED"/>
    <path d="M64 116 a20 20 0 0 1 20 -20 h296 a20 20 0 0 1 20 20 v20 h-336 z" fill="#F5F5F7"/>
    <circle cx="90" cy="116" r="5" fill="#D2D2D7"/><circle cx="108" cy="116" r="5" fill="#D2D2D7"/>
    <circle cx="126" cy="116" r="5" fill="#D2D2D7"/>
    <text x="232" y="121" font-family="{_FONT}" font-size="13" fill="#86868B"
          text-anchor="middle">june_purchases.xlsx</text>
    {messy}
  </g>

  <!-- center: Kira node -->
  <g filter="url(#ksh)">
    <rect x="486" y="196" width="148" height="88" rx="22" fill="#157A5B"/>
    <text x="560" y="248" font-family="{_FONT}" font-size="30" font-weight="600"
          fill="#FFFFFF" text-anchor="middle">Kira</text>
  </g>
  <path d="M446 240 h30" stroke="#C9C9CF" stroke-width="2" stroke-linecap="round"/>
  <path d="M470 232 l8 8 l-8 8" stroke="#C9C9CF" stroke-width="2" fill="none"
        stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M644 240 h30" stroke="#C9C9CF" stroke-width="2" stroke-linecap="round"/>
  <path d="M668 232 l8 8 l-8 8" stroke="#C9C9CF" stroke-width="2" fill="none"
        stroke-linecap="round" stroke-linejoin="round"/>

  <!-- right: clean SQL Accounting -->
  <g filter="url(#ksh)" transform="rotate(2.2 870 240)">
    <rect x="700" y="96" width="356" height="300" rx="20" fill="#FFFFFF" stroke="#E8E8ED"/>
    <path d="M700 116 a20 20 0 0 1 20 -20 h316 a20 20 0 0 1 20 20 v20 h-356 z" fill="#F5F5F7"/>
    <circle cx="726" cy="116" r="5" fill="#D2D2D7"/><circle cx="744" cy="116" r="5" fill="#D2D2D7"/>
    <circle cx="762" cy="116" r="5" fill="#D2D2D7"/>
    <text x="878" y="121" font-family="{_FONT}" font-size="13" fill="#86868B"
          text-anchor="middle">SQL Accounting</text>
    {clean}
  </g>
</svg>
"""


def compute_impact() -> dict:
    return impact_from_rows(firm_overview())


def impact_from_rows(rows: list[dict]) -> dict:
    lines = sum(int(r["lines_posted"] or 0) for r in rows)
    accs = [r["auto_accuracy"] for r in rows if r["auto_accuracy"] is not None]
    return {
        "clients": len(rows),
        "rules": sum(int(r["learned_rules"] or 0) for r in rows),
        "lines": lines,
        "hours": round(lines * _SECONDS_PER_LINE / 3600, 1),
        "accuracy": round(sum(accs) / len(accs), 3) if accs else None,
    }


_HEADLINE_LINES = ["Your books, posted to SQL.", "Without keying a single line."]


def _headline_iframe() -> str:
    lines = "".join(
        f'<span class="line" data-text="{t}">{t}</span>' for t in _HEADLINE_LINES)
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
*{{margin:0;box-sizing:border-box}}
html,body{{background:transparent;font-family:{_FONT};-webkit-font-smoothing:antialiased}}
.wrap{{text-align:center;padding-top:4px}}
.eyebrow{{font-size:15px;font-weight:600;color:#157A5B;margin-bottom:14px}}
.title{{font-size:clamp(38px,6.4vw,74px);font-weight:700;line-height:1.06;
  letter-spacing:-.035em;color:#1D1D1F}}
.title .line{{display:block}}
</style></head><body>
<div class="wrap">
  <p class="eyebrow">Kira for SQL Accounting</p>
  <h1 class="title">{lines}</h1>
</div>
<script>
const CH="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
function scramble(el,text,duration,speed){{
  const steps=Math.ceil(duration/speed);let step=0;
  const iv=setInterval(()=>{{
    const progress=step/steps;let out="";
    for(let i=0;i<text.length;i++){{
      if(text[i]===" "){{out+=" ";continue;}}
      out+=(progress*text.length>i)?text[i]:CH[Math.floor(Math.random()*CH.length)];
    }}
    step++;
    if(step>steps){{clearInterval(iv);el.textContent=text;}}else{{el.textContent=out;}}
  }},speed*1000);
}}
const lines=[...document.querySelectorAll(".line")];
const reduce=window.matchMedia("(prefers-reduced-motion: reduce)").matches;
if(reduce||sessionStorage.getItem("kira_hero_done")){{
  lines.forEach(el=>el.textContent=el.dataset.text);
}}else{{
  sessionStorage.setItem("kira_hero_done","1");
  lines.forEach((el,i)=>setTimeout(()=>scramble(el,el.dataset.text,1.0,0.03),i*160));
}}
</script></body></html>"""


def hero(impact: dict) -> None:
    if impact["lines"] > 0:
        acc = f"{impact['accuracy']:.0%}" if impact["accuracy"] is not None else "—"
        stats = [(f"{impact['lines']:,}", "lines converted"),
                 (f"{impact['hours']:,}", "hours of keying saved"),
                 (acc, "auto-coded, no touch")]
    else:
        stats = [(str(impact["clients"]), "client books ready"),
                 (str(impact["rules"]), "coding rules learned"),
                 ("7", "file types accepted")]
    stat_html = "".join(
        f'<div class="s"><b>{v}</b><span>{lbl}</span></div>' for v, lbl in stats)

    # Animated headline needs JS, so it lives in a self-contained component iframe.
    st.components.v1.html(_headline_iframe(), height=230, scrolling=False)

    st.markdown(f"""
<div class="kira-hero">
  <p class="kira-sub">Drop a messy Excel, a receipt photo, a forwarded invoice.
  Kira reads it, codes it to this client&rsquo;s own accounts, checks every line,
  and posts it into SQL Accounting. You just approve.</p>
  <div class="kira-visual">{_hero_svg()}</div>
  <div class="kira-stats">{stat_html}</div>
</div>
""", unsafe_allow_html=True)


def features() -> None:
    doc = ('<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 '
           '2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/><path d="M9 13h6"/>'
           '<path d="M9 17h4"/></svg>')
    tag = ('<svg viewBox="0 0 24 24"><path d="M20.6 13.4 12 22l-9-9V4a1 1 0 0 1 '
           '1-1h9z"/><circle cx="8" cy="8" r="1.4"/><path d="M13 15l2 2 4-4"/></svg>')
    shield = ('<svg viewBox="0 0 24 24"><path d="M12 3l8 3v5c0 5-3.5 8.5-8 10-4.5'
              '-1.5-8-5-8-10V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/></svg>')
    st.markdown(f"""
<div class="kira-features">
  <div class="kira-feat">{doc}
    <h3>Any format, as-is</h3>
    <p>Self-kept Excel books, CSVs, PDF invoices, WhatsApp receipt photos. No
    template to fill in first.</p>
  </div>
  <div class="kira-feat">{tag}
    <h3>Coded to their ledger</h3>
    <p>Each line mapped to this client&rsquo;s own chart of accounts and tax
    codes &mdash; and it learns their habits every batch.</p>
  </div>
  <div class="kira-feat">{shield}
    <h3>Checked before it posts</h3>
    <p>Duplicates, impossible tax, unknown codes and future dates are caught
    before anything reaches SQL.</p>
  </div>
</div>
""", unsafe_allow_html=True)


def drop_label() -> None:
    st.markdown('<p class="kira-droplabel">Drop a client&rsquo;s files to begin</p>',
                unsafe_allow_html=True)


def sidebar_brand() -> None:
    st.sidebar.markdown(
        '<div class="kira-side-wordmark">Kira<span class="dot">.</span></div>'
        '<p class="kira-side-tag">Excel and documents into SQL Accounting</p>',
        unsafe_allow_html=True)


def sidebar_status(ai_on: bool, posting_label: str, posting_state: str,
                   suppliers: int, accounts: int, rules: int) -> None:
    ai = ('<span class="dot ok"></span>AI coding <b>on</b>' if ai_on
          else '<span class="dot off"></span>AI coding <b>off</b> '
               '&mdash; set ANTHROPIC_API_KEY')
    post = f'<span class="dot {posting_state}"></span>Posting <b>{posting_label}</b>'
    st.sidebar.markdown(
        f'<div class="kira-status">{ai}</div>'
        f'<div class="kira-status">{post}</div>'
        f'<p class="kira-side-meta">{suppliers} suppliers &middot; {accounts} accounts'
        f'<br>{rules} learned rules</p>',
        unsafe_allow_html=True)
