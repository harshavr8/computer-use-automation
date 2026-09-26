"""Flask app for the mock legacy member-servicing system.

Flows:
  1. Sign on -> member search -> member detail -> read savings balance
  2. Member detail -> open sub-account form -> review -> confirm -> confirmation #

Error/exception states (for replay to detect):
  business : MBR-404 member not found, SEC-403 restricted record,
             ERR-104 / ERR-2xx validation errors
  recoverable : System Notice overlay, slow pages, one-off APP-500
  session  : idle timeout -> bounced to sign-on
"""
from __future__ import annotations

import os
import re
import secrets
import time
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import (
    Flask, abort, jsonify, redirect, render_template, request, session, url_for,
)

from .data import PRODUCTS, Store
from .faults import FaultConfig
from .variants import VARIANTS

MEMBER_ID_RE = re.compile(r"^\d{5}$")


def create_app(
    variant: str = "base",
    faults: FaultConfig | None = None,
    enable_admin: bool = True,
) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("MOCK_SECRET_KEY") or secrets.token_hex(16)
    if variant not in VARIANTS:
        raise ValueError(f"unknown variant {variant!r}; choose from {sorted(VARIANTS)}")

    v = VARIANTS[variant]
    fx = faults or FaultConfig.from_env()
    store = Store()
    # Fake operator credentials for the mock only. Override via env; never real creds.
    user = os.environ.get("MOCK_USER", "teller")
    password = os.environ.get("MOCK_PASS", "demo-only")

    app.config.update(VARIANT=v, FAULTS=fx, STORE=store)

    @app.context_processor
    def inject_globals():
        return {"v": v}

    # ---- fault hooks -------------------------------------------------------
    def _is_content(path: str) -> bool:
        return not path.startswith(("/__admin", "/static"))

    @app.before_request
    def apply_faults():
        if not _is_content(request.path):
            return None
        if fx.slow_ms:
            time.sleep(fx.slow_ms / 1000)
        if fx.fail_next > 0 and (not fx.fail_route or fx.fail_route in request.path):
            fx.fail_next -= 1
            return render_template("app_error.html"), 500
        return None

    # ---- auth / session ----------------------------------------------------
    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("user"):
                return redirect(url_for("login"))
            # expire_next fires on a transaction submit (POST), where real apps check the session
            forced = fx.expire_next > 0 and request.method == "POST" and request.path.startswith("/members")
            if forced:
                fx.expire_next -= 1
            if forced or time.time() - session.get("last_seen", 0) > fx.session_timeout_s:
                session.clear()
                return redirect(url_for("login", expired=1))
            session["last_seen"] = time.time()
            return view(*args, **kwargs)
        return wrapper

    @app.route("/")
    def root():
        return redirect(url_for("shell") if session.get("user") else url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            if request.form.get("f1") == user and request.form.get("f2") == password:
                session.clear()
                session.update(user=user, last_seen=time.time())
                return redirect(url_for("shell"))
            error = "SGN-001 INVALID OPERATOR ID OR PASSWORD"
        return render_template("login.html", error=error, expired=request.args.get("expired"))

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/app")
    @login_required
    def shell():
        return render_template("shell.html")

    @app.route("/nav")
    @login_required
    def nav():
        return render_template("nav.html")

    # ---- flow 1: search -> detail -------------------------------------------
    @app.route("/members/search", methods=["GET", "POST"])
    @login_required
    def member_search():
        q = (request.form.get("q1") or "").strip()
        error = None
        if request.method == "POST":
            if not MEMBER_ID_RE.match(q):
                error = f"ERR-104 {v.member_label.upper()} MUST BE 5 DIGITS"
            elif store.get(q) is None:
                error = f"MBR-404 NO MEMBER FOUND MATCHING {q}"
            else:
                return redirect(url_for("member_detail", member_id=q))
        return render_template("search.html", q=q, error=error)

    @app.route("/members/<member_id>")
    @login_required
    def member_detail(member_id: str):
        member = store.get(member_id)
        if member is None:
            abort(404)
        if member["restricted"]:
            return render_template("denied.html", member_id=member_id), 403
        return render_template(
            "detail.html", m=member, member_id=member_id, notice=fx.notice_dialog,
        )

    # ---- flow 2: open sub-account ------------------------------------------
    def _member_or_404(member_id: str) -> dict:
        member = store.get(member_id)
        if member is None or member["restricted"]:
            abort(404)
        return member

    @app.route("/members/<member_id>/subacct", methods=["GET", "POST"])
    @login_required
    def subacct_new(member_id: str):
        member = _member_or_404(member_id)
        savings = store.savings(member)
        form = {"p": "", "n": "", "amt": ""}
        error = None
        if request.method == "POST":
            form = {k: (request.form.get(k) or "").strip() for k in form}
            error = _validate_subacct(form, savings)
            if error is None:
                session["pending_subacct"] = {"member_id": member_id, **form}
                return redirect(url_for("subacct_review", member_id=member_id))
        return render_template(
            "subacct_form.html", m=member, member_id=member_id, savings=savings,
            products=PRODUCTS, form=form, error=error,
        )

    def _validate_subacct(form: dict, savings: dict | None) -> str | None:
        if form["p"] not in PRODUCTS:
            return "ERR-210 SELECT A PRODUCT TYPE"
        try:
            amt = Decimal(form["amt"].replace(",", "").replace("$", ""))
        except InvalidOperation:
            return "ERR-211 OPENING DEPOSIT MUST BE A NUMBER"
        if amt < PRODUCTS[form["p"]]["min"]:
            return f"ERR-212 MINIMUM OPENING DEPOSIT FOR {form['p']} IS ${PRODUCTS[form['p']]['min']:,.2f}"
        if savings is None or amt > savings["balance"]:
            return "ERR-221 OPENING DEPOSIT EXCEEDS AVAILABLE BALANCE IN S00"
        if len(form["n"]) > 20:
            return "ERR-213 NICKNAME MAX 20 CHARACTERS"
        return None

    @app.route("/members/<member_id>/subacct/review")
    @login_required
    def subacct_review(member_id: str):
        member = _member_or_404(member_id)
        pending = session.get("pending_subacct")
        if not pending or pending["member_id"] != member_id:
            return redirect(url_for("subacct_new", member_id=member_id))
        return render_template(
            "subacct_review.html", m=member, member_id=member_id, pending=pending,
            product=PRODUCTS[pending["p"]], native_confirm=fx.native_confirm,
        )

    @app.route("/members/<member_id>/subacct/confirm", methods=["POST"])
    @login_required
    def subacct_confirm(member_id: str):
        member = _member_or_404(member_id)
        pending = session.pop("pending_subacct", None)
        if not pending or pending["member_id"] != member_id:
            return redirect(url_for("subacct_new", member_id=member_id))
        # Re-validate at commit time; balances may have changed since review.
        error = _validate_subacct(pending, store.savings(member))
        if error:
            return render_template(
                "subacct_form.html", m=member, member_id=member_id,
                savings=store.savings(member), products=PRODUCTS, form=pending, error=error,
            )
        amount = Decimal(pending["amt"].replace(",", "").replace("$", ""))
        conf = store.open_subaccount(member, pending["p"], pending["n"], amount)
        return render_template(
            "subacct_done.html", m=member, member_id=member_id, conf=conf,
            product=PRODUCTS[pending["p"]], amount=amount,
        )

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("app_error.html", code="APP-404",
                               detail="RESOURCE NOT FOUND"), 404

    # ---- admin (test harness only; outside the agent's allowlist) -----------
    if enable_admin:
        @app.route("/__admin/faults", methods=["GET", "POST"])
        def admin_faults():
            if request.method == "POST":
                try:
                    fx.update(request.get_json(force=True) or {})
                except ValueError as exc:
                    return jsonify(error=str(exc)), 400
            return jsonify(fx.to_dict())

        @app.route("/__admin/reset", methods=["POST"])
        def admin_reset():
            store.reset()
            fx.update(FaultConfig().to_dict())
            return jsonify(ok=True)

    return app
