"""Per-client learned coding rules.

Every approved line becomes evidence: normalized supplier name ->
(supplier_code, account_code, tax_code) with a vote count per distinct
coding. lookup() returns the majority coding, so one odd correction
doesn't overwrite months of consistent history. Batch 2 needs less
review than batch 1 — this file is the flywheel.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


def normalize_supplier(name: str) -> str:
    s = re.sub(r"[^a-z0-9一-鿿 ]", " ", str(name).lower())
    s = re.sub(
        r"\b(sdn|bhd|s/b|enterprise|trading|ent|plt|resources|services|store|shop|kedai)\b",
        " ",
        s,
    )
    return re.sub(r"\s+", " ", s).strip()


class RuleStore:
    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "rules.json"
        # {"doc_type|normalized_party": {"votes": {"party|acct|tax": count}}}
        self.rules: dict[str, dict] = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for key, val in data.items():
                if "|" not in key:  # migrate: un-typed keys were purchases
                    key = f"purchase|{key}"
                if "votes" in val:
                    self.rules[key] = val
                else:  # migrate v1 single-coding format
                    combo = f"{val.get('supplier_code','')}|{val.get('account_code','')}|{val.get('tax_code','')}"
                    self.rules[key] = {"votes": {combo: val.get("count", 1)}}

    def __len__(self) -> int:
        return len(self.rules)

    @staticmethod
    def _key(supplier_name: str, doc_type: str) -> str:
        return f"{doc_type or 'purchase'}|{normalize_supplier(supplier_name)}"

    def lookup(self, supplier_name: str, doc_type: str = "purchase") -> dict | None:
        entry = self.rules.get(self._key(supplier_name, doc_type))
        if not entry or not entry["votes"]:
            return None
        combo, count = max(entry["votes"].items(), key=lambda kv: kv[1])
        total = sum(entry["votes"].values())
        sup, acct, tax = combo.split("|")
        return {
            "supplier_code": sup,
            "account_code": acct,
            "tax_code": tax,
            "count": count,
            "consistency": round(count / total, 2),  # 1.0 = always coded the same
        }

    def learn(self, supplier_name: str, supplier_code: str,
              account_code: str, tax_code: str,
              doc_type: str = "purchase") -> None:
        if not normalize_supplier(supplier_name):
            return
        combo = f"{supplier_code}|{account_code}|{tax_code}"
        entry = self.rules.setdefault(self._key(supplier_name, doc_type),
                                      {"votes": {}})
        entry["votes"][combo] = entry["votes"].get(combo, 0) + 1

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.rules, indent=2, ensure_ascii=False), encoding="utf-8"
        )
