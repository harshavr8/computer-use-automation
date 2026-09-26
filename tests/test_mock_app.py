"""Tests for the mock target: every error state replay will depend on must be reachable."""
from __future__ import annotations

import pytest

from mock_app import create_app
from mock_app.faults import FaultConfig


@pytest.fixture
def make_client():
    def _make(variant="base", **faults):
        app = create_app(variant=variant, faults=FaultConfig.from_dict(faults))
        client = app.test_client()
        client.post("/login", data={"f1": "teller", "f2": "demo-only"})
        return app, client
    return _make


def search(client, member_id):
    return client.post("/members/search", data={"q1": member_id})


def test_bad_login_is_rejected():
    client = create_app(faults=FaultConfig()).test_client()
    resp = client.post("/login", data={"f1": "teller", "f2": "wrong"})
    assert b"SGN-001" in resp.data


def test_unauthenticated_redirects_to_login():
    client = create_app(faults=FaultConfig()).test_client()
    resp = client.get("/members/search")
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_found_member_shows_savings_balance(make_client):
    _, c = make_client()
    resp = search(c, "12345")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/members/12345")
    page = c.get("/members/12345").data
    assert b"REGULAR SAVINGS" in page and b"$2,450.18" in page


def test_not_found_is_a_page_message_not_an_http_error(make_client):
    _, c = make_client()
    resp = search(c, "99999")
    assert resp.status_code == 200  # business outcome: app renders normally
    assert b"MBR-404 NO MEMBER FOUND MATCHING 99999" in resp.data


def test_validation_error(make_client):
    _, c = make_client()
    assert b"ERR-104" in search(c, "12ab").data


def test_restricted_record_is_denied(make_client):
    _, c = make_client()
    search(c, "55555")
    resp = c.get("/members/55555")
    assert resp.status_code == 403 and b"SEC-403" in resp.data


def test_open_subaccount_happy_path(make_client):
    _, c = make_client()
    resp = c.post("/members/12345/subacct", data={"p": "HC", "n": "gifts", "amt": "50"})
    assert resp.headers["Location"].endswith("/subacct/review")
    assert b"Confirm &amp; Open" in c.get("/members/12345/subacct/review").data
    done = c.post("/members/12345/subacct/confirm").data
    assert b"SA-100231" in done
    detail = c.get("/members/12345").data
    assert b"$2,400.18" in detail and b"HOLIDAY CLUB - GIFTS" in detail


def test_insufficient_funds_validation(make_client):
    _, c = make_client()
    resp = c.post("/members/23456/subacct", data={"p": "HC", "n": "", "amt": "100"})
    assert b"ERR-221" in resp.data


def test_certificate_minimum(make_client):
    _, c = make_client()
    resp = c.post("/members/12345/subacct", data={"p": "CD12", "n": "", "amt": "100"})
    assert b"ERR-212" in resp.data


def test_confirm_is_not_replayable_twice(make_client):
    _, c = make_client()
    c.post("/members/12345/subacct", data={"p": "HC", "n": "", "amt": "10"})
    c.post("/members/12345/subacct/confirm")
    again = c.post("/members/12345/subacct/confirm")
    assert again.status_code == 302  # pending cleared: no double-open


def test_fail_next_is_deterministic_and_scoped(make_client):
    _, c = make_client(fail_next=1, fail_route="/members/12345")
    assert c.get("/members/search").status_code == 200  # out of scope, not consumed
    first = c.get("/members/12345")
    assert first.status_code == 500 and b"APP-500" in first.data
    assert c.get("/members/12345").status_code == 200  # recovers on retry


def test_notice_dialog_overlay(make_client):
    _, c = make_client(notice_dialog=True)
    assert b"System Notice" in c.get("/members/12345").data


def test_native_confirm_flag(make_client):
    _, c = make_client(native_confirm=True)
    c.post("/members/12345/subacct", data={"p": "HC", "n": "", "amt": "10"})
    assert b"return confirm(" in c.get("/members/12345/subacct/review").data


def test_session_timeout(make_client, monkeypatch):
    _, c = make_client(session_timeout_s=60)
    import mock_app.app as appmod
    real = appmod.time.time
    monkeypatch.setattr(appmod.time, "time", lambda: real() + 120)
    resp = c.get("/members/search")
    assert resp.status_code == 302 and "expired=1" in resp.headers["Location"]


def test_admin_faults_toggle_and_reset(make_client):
    app, c = make_client()
    assert c.post("/__admin/faults", json={"notice_dialog": True}).json["notice_dialog"] is True
    assert c.post("/__admin/faults", json={"bogus": 1}).status_code == 400
    c.post("/__admin/reset")
    assert app.config["FAULTS"].notice_dialog is False


def test_tenant_variant_relabels_same_flow(make_client):
    _, c = make_client(variant="tenant_b")
    page = c.get("/members/search").data
    assert b"Account #:" in page and b'value="Find"' in page
    assert b"ERR-104 ACCOUNT # MUST BE 5 DIGITS" in search(c, "x").data


def test_markup_has_no_ids_or_test_ids(make_client):
    _, c = make_client()
    for path in ["/members/search", "/members/12345", "/members/12345/subacct"]:
        html = c.get(path).data.lower()
        assert b" id=" not in html and b"data-testid" not in html


def test_expire_next_is_one_shot(make_client):
    _, c = make_client(expire_next=1)
    assert c.get("/members/search").status_code == 200          # page views are unaffected
    first = search(c, "12345")
    assert first.status_code == 302 and "expired=1" in first.headers["Location"]
    c.post("/login", data={"f1": "teller", "f2": "demo-only"})
    assert search(c, "12345").headers["Location"].endswith("/members/12345")
