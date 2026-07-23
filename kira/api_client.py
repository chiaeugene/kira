"""HTTP client for a remote Kira Cloud — lets the console run anywhere.

Activated by setting:
  KIRA_API_URL    e.g. https://kira-cloud.onrender.com
  KIRA_FIRM_TOKEN the firm token configured on the server

Every function mirrors a server endpoint 1:1.
"""

from __future__ import annotations

import os

import httpx


def remote_url() -> str | None:
    return os.environ.get("KIRA_API_URL") or None


class KiraAPI:
    def __init__(self, base_url: str, firm_token: str):
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"), timeout=300,
            headers={"Authorization": f"Bearer {firm_token}"})

    @classmethod
    def from_env(cls) -> "KiraAPI":
        return cls(os.environ["KIRA_API_URL"],
                   os.environ.get("KIRA_FIRM_TOKEN", ""))

    def _get(self, path: str, **params):
        r = self.http.get(path, params={k: v for k, v in params.items()
                                        if v is not None})
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, json: dict | None = None):
        r = self.http.post(path, json=json)
        if r.status_code in (409, 422):
            detail = r.json().get("detail")
            if isinstance(detail, dict):
                return {"_conflict": True, **detail}
            return {"_conflict": True, "message": detail}
        r.raise_for_status()
        return r.json()

    # ---- surface used by the console ----
    def health(self) -> dict:
        return self._get("/api/health")

    def clients(self) -> list[dict]:
        return self._get("/api/clients")

    def create_client(self, name: str) -> dict:
        return self._post("/api/clients", {"name": name})

    def upload_masters(self, client: str, files: dict[str, bytes]) -> dict:
        """files: {"chart_of_accounts.csv": bytes, "suppliers.csv": bytes, ...}"""
        field_map = {"chart_of_accounts.csv": "chart_of_accounts",
                    "suppliers.csv": "suppliers",
                    "customers.csv": "customers",
                    "tax_codes.csv": "tax_codes"}
        multipart = {field_map[f]: (f, content, "text/csv")
                    for f, content in files.items() if f in field_map}
        r = self.http.post(f"/api/clients/{client}/masters", files=multipart)
        r.raise_for_status()
        return r.json()

    def upload(self, client: str, files: list[tuple[str, bytes]]) -> dict:
        r = self.http.post(f"/api/clients/{client}/upload",
                           files=[("files", (n, d)) for n, d in files])
        if r.status_code == 422:
            return {"_unparseable": True, **(r.json().get("detail") or {})}
        r.raise_for_status()
        return r.json()

    def batches(self, client: str | None = None, state: str | None = None):
        return self._get("/api/batches", client=client, state=state)

    def batch(self, bid: str) -> dict:
        return self._get(f"/api/batches/{bid}")

    def repairs(self, bid: str) -> list[dict]:
        return self._get(f"/api/batches/{bid}/repairs")

    def approve(self, bid: str, rows: list[dict]) -> dict:
        return self._post(f"/api/batches/{bid}/approve", {"rows": rows})

    def reject(self, bid: str, reason: str = "") -> dict:
        return self._post(f"/api/batches/{bid}/reject", {"reason": reason})

    def agents(self) -> dict:
        return self._get("/api/agents")

    def overview(self) -> dict:
        return self._get("/api/firm/overview")

    def history(self, client: str) -> dict:
        return self._get(f"/api/clients/{client}/history")
