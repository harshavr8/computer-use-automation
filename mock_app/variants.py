"""Tenant variants: the same vendor product, configured and branded differently.

This is the stand-in for "hundreds of tenants running the same vendor product".
Only labels/branding differ; structure and flows are identical. Used later for
the cross-tenant reuse stretch goal (one artifact + per-variant overrides).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Variant:
    key: str
    brand: str
    product_version: str
    member_label: str
    search_button: str
    balance_header: str
    open_subacct_link: str


VARIANTS: dict[str, Variant] = {
    "base": Variant(
        key="base",
        brand="FIRST COMMUNITY CREDIT UNION",
        product_version="MbrSvc 4.2.1",
        member_label="Member Number",
        search_button="Search",
        balance_header="Balance",
        open_subacct_link="Open Sub-Account",
    ),
    "tenant_b": Variant(
        key="tenant_b",
        brand="LAKESHORE FEDERAL CU",
        product_version="MbrSvc 4.3.0",
        member_label="Account #",
        search_button="Find",
        balance_header="Current Bal",
        open_subacct_link="Add Share",
    ),
}
