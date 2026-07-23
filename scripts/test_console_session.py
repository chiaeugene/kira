"""Console session tests (the 'bouncing' bug) using Streamlit's AppTest.

Reproduces the exact reported flow: log in -> switch client -> upload state
present -> switch again. The bug was st.session_state.clear() wiping the
login (authed) and the dropdown selection on every client switch, throwing
the user back to the sign-in page ("bouncing").
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

# Force LOCAL mode (no network) + activate the password gate like the
# hosted console. Must be set before app.py's load_env() runs.
os.environ["KIRA_API_URL"] = ""          # empty -> remote_url() returns None
os.environ["KIRA_CONSOLE_PASSWORD"] = "pw123"

from streamlit.testing.v1 import AppTest


def ss(at: AppTest, key: str):
    """AppTest's session_state proxy has no .get()."""
    try:
        return at.session_state[key]
    except KeyError:
        return None


def new_app() -> AppTest:
    at = AppTest.from_file("app.py", default_timeout=60)
    at.run()
    return at


print("[1] first load shows the login gate")
at = new_app()
assert len(at.text_input) == 1, "expected only the password field"
assert not at.sidebar.selectbox, "app content must be hidden before login"

print("[2] wrong password -> error, still locked out")
at.text_input[0].set_value("nope")
at.button[0].set_value(True).run()
assert any("Wrong password" in e.value for e in at.error)
assert not ss(at, "authed")

print("[3] correct password -> in")
at.text_input[0].set_value("pw123")
at.button[0].set_value(True).run()
assert ss(at, "authed") is True
assert at.sidebar.selectbox, "app should render after login"
first_client = at.sidebar.selectbox(key="client_select").value
print(f"    landed on client: {first_client}")

# Fresh session with authed pre-seeded for the switching tests — AppTest
# can't re-serialize the login form once it has left the page.
at = AppTest.from_file("app.py", default_timeout=60)
at.session_state["authed"] = True
at.run()
first_client = at.sidebar.selectbox(key="client_select").value

print("[4] THE BUG: switching client must NOT log out or bounce")
other = "SRI_MURNI_TRADING" if first_client != "SRI_MURNI_TRADING" else "DEMO_CLIENT"
at.sidebar.selectbox(key="client_select").select(other).run()
assert ss(at, "authed") is True, "BOUNCE: login was wiped!"
assert at.sidebar.selectbox(key="client_select").value == other, \
    "BOUNCE: dropdown snapped back to the default client!"
assert ss(at, "client") == other
print(f"    switched to {other}, still logged in, selection stuck")

print("[5] switching clears per-client work state but nothing else")
at.session_state["coded"] = "sentinel-df"
at.session_state["rows_b_x"] = "sentinel-rows"
at.session_state["some_unrelated"] = "keep-me"
back = first_client
at.sidebar.selectbox(key="client_select").select(back).run()
assert ss(at, "coded") is None, "stale work must be cleared"
assert ss(at, "rows_b_x") is None
assert ss(at, "some_unrelated") == "keep-me"
assert ss(at, "authed") is True
print("    work state cleared, login + other state preserved")

print("[6] several interactions in a row stay logged in (no bouncing)")
for _ in range(4):
    at.sidebar.selectbox(key="client_select").select(other).run()
    at.sidebar.selectbox(key="client_select").select(back).run()
assert ss(at, "authed") is True
assert not at.exception, [str(e.value) for e in at.exception]
print("    8 switches, zero bounces, zero exceptions")

print("\nALL CONSOLE SESSION TESTS PASSED")
