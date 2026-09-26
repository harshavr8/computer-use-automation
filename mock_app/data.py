"""In-memory seed data. Every person here is fictional; phone numbers use 555-01xx."""
from __future__ import annotations

import copy
from decimal import Decimal

PRODUCTS = {
    "HC": {"desc": "HOLIDAY CLUB", "min": Decimal("5.00")},
    "VC": {"desc": "VACATION CLUB", "min": Decimal("5.00")},
    "CD12": {"desc": "12 MO SHARE CERTIFICATE", "min": Decimal("500.00")},
}

_SEED = {
    "12345": {
        "name": "JANE Q SAMPLE",
        "since": "03/02/2014",
        "ssn_last4": "0000",
        "phone": "(414) 555-0142",
        "restricted": False,
        "shares": [
            {"id": "S00", "desc": "REGULAR SAVINGS", "balance": Decimal("2450.18"), "status": "OPEN"},
            {"id": "S10", "desc": "SHARE DRAFT CHECKING", "balance": Decimal("812.40"), "status": "OPEN"},
        ],
    },
    "23456": {
        "name": "JOHN R EXAMPLE",
        "since": "11/19/2020",
        "ssn_last4": "0001",
        "phone": "(414) 555-0187",
        "restricted": False,
        "shares": [
            {"id": "S00", "desc": "REGULAR SAVINGS", "balance": Decimal("15.00"), "status": "OPEN"},
        ],
    },
    "55555": {
        "name": "RESTRICTED RECORD",
        "since": "01/01/2001",
        "ssn_last4": "0002",
        "phone": "(414) 555-0199",
        "restricted": True,  # staff/insider account: teller role may not view
        "shares": [],
    },
}


class Store:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.members = copy.deepcopy(_SEED)
        self.next_confirmation = 100231  # deterministic confirmation numbers

    def get(self, member_id: str) -> dict | None:
        return self.members.get(member_id)

    def savings(self, member: dict) -> dict | None:
        return next((s for s in member["shares"] if s["id"] == "S00"), None)

    def open_subaccount(self, member: dict, product: str, nickname: str, amount: Decimal) -> str:
        savings = self.savings(member)
        assert savings is not None
        savings["balance"] -= amount
        share_id = f"S{20 + len(member['shares']):02d}"
        desc = PRODUCTS[product]["desc"] + (f" - {nickname.upper()}" if nickname else "")
        member["shares"].append({"id": share_id, "desc": desc, "balance": amount, "status": "OPEN"})
        conf = f"SA-{self.next_confirmation}"
        self.next_confirmation += 1
        return conf
