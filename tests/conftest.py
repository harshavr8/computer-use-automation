"""Shared fixtures: a live mock app on a random port, and a Playwright driver."""
from __future__ import annotations

import json
import threading
import urllib.request

import pytest
from werkzeug.serving import make_server

from mock_app import create_app
from mock_app.faults import FaultConfig


class LiveApp:
    def __init__(self, variant: str) -> None:
        self.app = create_app(variant=variant, faults=FaultConfig())
        self.server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _post(self, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body or {}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())

    def faults(self, **kw) -> dict:
        return self._post("/__admin/faults", kw)

    def reset(self) -> None:
        self._post("/__admin/reset")

    def stop(self) -> None:
        self.server.shutdown()


@pytest.fixture(scope="session")
def live_app():
    app = LiveApp("base")
    yield app
    app.stop()


@pytest.fixture(scope="session")
def live_app_b():
    app = LiveApp("tenant_b")
    yield app
    app.stop()


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def browser_ok():
    if not _chromium_available():
        pytest.skip("Playwright Chromium not installed: run `python -m playwright install chromium`")
