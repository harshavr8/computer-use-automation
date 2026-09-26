"""Web SurfaceDriver on Playwright (Chromium), built for legacy markup.

Design notes
- Frame-aware: accessibility snapshots do not descend into iframes, so observe()
  walks every frame and every Target names the frame it lives in.
- Consensus resolution: all strategies of a Target are evaluated; the best-ranked
  unique match wins, and any other unique match that points at a *different*
  control raises TargetAmbiguous. Non-matching strategies are a drift signal.
- Native JS dialogs are never auto-accepted. They are dismissed and recorded
  unless replay explicitly armed an expectation (pattern + accept) beforehand.
- Waiting: resolution polls until a strategy matches (handles slow pages);
  replay should additionally wait on checkpoints, not on time.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import Dialog, Frame, Locator, Page, sync_playwright

from ..core.actions import Action
from ..core.observation import DialogEvent, ElementInfo, FrameView, Observation
from ..core.targets import (
    AnchorTextStrategy, FieldNameStrategy, RoleStrategy, Strategy, TableCellStrategy,
    Target, TextStrategy,
)
from .base import FrameNotFound, Resolved, StrategyCheck, TargetAmbiguous, TargetNotFound

INTERACTIVE = "input:not([type=hidden]), select, textarea, button, a[href]"

_CONTROL_XPATH = {
    "textbox": "*[(self::input and (not(@type) or @type='text' or @type='password' or @type='email'"
               " or @type='number' or @type='tel' or @type='search')) or self::textarea]",
    "combobox": "select",
    "button": "*[self::button or (self::input and (@type='submit' or @type='button' or @type='reset'))]",
    "link": "a[@href]",
    "checkbox": "input[@type='checkbox']",
    "radio": "input[@type='radio']",
}

_INDEX_JS = r"""
(sel) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const visible = e => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const role = e => { const t = e.tagName.toLowerCase(); const ty = (e.getAttribute('type') || 'text').toLowerCase();
    if (t === 'a') return 'link'; if (t === 'select') return 'combobox'; if (t === 'textarea') return 'textbox';
    if (t === 'button' || ['submit','button','reset','image'].includes(ty)) return 'button';
    if (ty === 'checkbox') return 'checkbox'; if (ty === 'radio') return 'radio'; return 'textbox'; };
  const ownText = e => { const t = e.tagName.toLowerCase();
    if (t === 'input') return ['submit','button','reset'].includes((e.type || '').toLowerCase()) ? norm(e.value) : '';
    if (t === 'select' || t === 'textarea') return ''; return norm(e.innerText); };
  const accName = e => norm(e.getAttribute('aria-label')) || (e.labels && e.labels.length ? norm(e.labels[0].innerText) : '')
    || ownText(e) || norm(e.getAttribute('title'));
  const anchor = e => {
    if (e.labels && e.labels.length) return norm(e.labels[0].innerText);
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    let last = '';
    while (w.nextNode()) { const n = w.currentNode;
      if (e.contains(n) || (e.compareDocumentPosition(n) & Node.DOCUMENT_POSITION_FOLLOWING)) break;
      const p = n.parentElement; if (p && ['SCRIPT','STYLE','OPTION'].includes(p.tagName)) continue;
      const t = norm(n.textContent); if (t) last = t; }
    return /[A-Za-z]/.test(last) ? last : ''; };
  return Array.from(document.querySelectorAll(sel)).map((e, i) => ({
    i, visible: visible(e), role: role(e), name: accName(e), anchor: anchor(e),
    field_name: e.getAttribute('name') || '', text: ownText(e) }));
}
"""

_CONTROL_TEXT_JS = r"""
e => { const t = e.tagName.toLowerCase(); const ty = (e.type || '').toLowerCase();
  if (t === 'input' && ['submit','button','reset'].includes(ty)) return e.value;
  if (t === 'input' || t === 'textarea' || t === 'select') return '';   // never echo typed data
  return (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200); }
"""


def xpath_literal(s: str) -> str:
    """Quote an arbitrary string for XPath 1.0."""
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in s.split("'")) + ")"


def strip_colon(s: str) -> str:
    return s.strip().rstrip(":").strip()


class WebPlaywrightDriver:
    def __init__(self, page: Page, base_url: str, resolve_timeout_ms: int = 5000) -> None:
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.resolve_timeout_ms = resolve_timeout_ms
        self._expected_dialog: tuple[re.Pattern[str], bool] | None = None
        self._dialogs: list[DialogEvent] = []
        self._pw = None
        self._browser = None
        page.on("dialog", self._on_dialog)

    # ---- lifecycle ------------------------------------------------------------
    @classmethod
    def launch(cls, base_url: str, headless: bool = True, **kw) -> "WebPlaywrightDriver":
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
        drv = cls(page, base_url, **kw)
        drv._pw, drv._browser = pw, browser
        return drv

    def close(self) -> None:
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    # ---- frames / urls ----------------------------------------------------------
    def _frame(self, name: str | None) -> Frame:
        if name is None:
            return self.page.main_frame
        frame = self.page.frame(name=name)
        if frame is None:
            raise FrameNotFound(
                f"frame {name!r} not present",
                available=[f.name for f in self.page.frames if f.name],
            )
        return frame

    def _named_frames(self) -> list[tuple[str | None, Frame]]:
        out: list[tuple[str | None, Frame]] = [(None, self.page.main_frame)]
        for i, f in enumerate(self.page.frames[1:]):
            out.append((f.name or f"frame{i}", f))
        return out

    def frame_url(self, frame: str | None) -> str:
        return self._frame(frame).url

    def url_for(self, route: str) -> str:
        return self.base_url + route

    def frame_urls(self) -> dict[str, str]:
        return {(name or "top"): f.url for name, f in self._named_frames()}

    # ---- dialogs ------------------------------------------------------------------
    def expect_dialog(self, pattern: str, accept: bool) -> None:
        """Arm a one-shot expectation for the next native dialog."""
        self._expected_dialog = (re.compile(pattern), accept)

    def _on_dialog(self, d: Dialog) -> None:
        exp = self._expected_dialog
        expected = bool(exp and exp[0].search(d.message))
        accept = bool(expected and exp and exp[1])
        self._expected_dialog = None
        d.accept() if accept else d.dismiss()
        self._dialogs.append(DialogEvent(d.type, d.message, "accepted" if accept else "dismissed", expected))

    def drain_dialogs(self) -> list[DialogEvent]:
        out, self._dialogs = self._dialogs, []
        return out

    # ---- observe ------------------------------------------------------------------
    def observe(self) -> Observation:
        views: list[FrameView] = []
        for fname, frame in self._named_frames():
            try:
                snapshot = frame.locator("body").aria_snapshot(timeout=2000)
                raw = frame.evaluate(_INDEX_JS, INTERACTIVE)
            except Exception:  # frame navigating/detached mid-observe
                continue
            elements = []
            for item in raw:
                if not item["visible"]:
                    continue
                el = ElementInfo(
                    ref=f"{fname or 'top'}:e{item['i']}", frame=fname, role=item["role"],
                    name=item["name"], anchor_text=strip_colon(item["anchor"]),
                    field_name=item["field_name"], text=item["text"],
                )
                el.candidates = self._validated_candidates(frame, item["i"], el)
                elements.append(el)
            views.append(FrameView(name=fname, url=frame.url, snapshot=snapshot, elements=elements))
        return Observation(url=self.page.url, title=self.page.title(), frames=views,
                           dialogs=self.drain_dialogs())

    def _validated_candidates(self, frame: Frame, index: int, el: ElementInfo) -> list[Strategy]:
        """Propose strategies for a control, keep only those that uniquely hit *it*."""
        proposals: list[Strategy] = []
        if el.name:
            proposals.append(RoleStrategy(role=el.role, name=el.name))  # type: ignore[arg-type]
        if el.anchor_text and el.anchor_text != el.name and el.role in ("textbox", "combobox", "checkbox", "radio"):
            proposals.append(AnchorTextStrategy(text=el.anchor_text, control=el.role))  # type: ignore[arg-type]
        if el.text and el.role in ("link", "button") and not el.name:
            proposals.append(TextStrategy(text=el.text))
        if el.field_name:
            proposals.append(FieldNameStrategy(name=el.field_name))
        me = frame.locator(INTERACTIVE).nth(index).element_handle()
        keep = []
        for s in proposals:
            loc, n = self._locate(frame, s)
            if n == 1 and loc is not None and loc.evaluate("(a, b) => a === b", me):
                keep.append(s)
        return keep

    # ---- resolve ----------------------------------------------------------------------
    def _locate(self, frame: Frame, s: Strategy) -> tuple[Locator | None, int]:
        match s:
            case RoleStrategy():
                loc = frame.get_by_role(s.role, name=s.name, exact=True)
            case TextStrategy():
                loc = frame.get_by_text(s.text, exact=True)
            case FieldNameStrategy():
                escaped = s.name.replace("\\", "\\\\").replace('"', '\\"')
                loc = frame.locator(f'[name="{escaped}"]')
            case AnchorTextStrategy():
                t = strip_colon(s.text)
                anchor = (f"//*[normalize-space(text())={xpath_literal(t)}"
                          f" or normalize-space(text())={xpath_literal(t + ':')}]")
                loc = frame.locator(f"xpath={anchor}/following::{_CONTROL_XPATH[s.control]}[1]")
            case TableCellStrategy():
                return self._locate_cell(frame, s)
            case _:
                return None, 0
        return loc, loc.count()

    def _locate_cell(self, frame: Frame, s: TableCellStrategy) -> tuple[Locator | None, int]:
        cell = "*[self::td or self::th]"
        header = frame.locator(f"xpath=//tr/{cell}[normalize-space(.)={xpath_literal(s.column_header)}]")
        row = frame.locator(f"xpath=//tr[{cell}[normalize-space(.)={xpath_literal(s.row_key)}]]")
        if header.count() != 1 or row.count() != 1:
            return None, 0 if header.count() == 0 or row.count() == 0 else 2
        col = header.evaluate("e => [...e.parentElement.children].filter(c => /^T[DH]$/.test(c.tagName)).indexOf(e)")
        loc = row.locator(f"xpath=./{cell}[{col + 1}]")
        return loc, loc.count()

    def resolve(self, target: Target, timeout_ms: int | None = None) -> Resolved:
        deadline = time.monotonic() + (timeout_ms or self.resolve_timeout_ms) / 1000
        while True:
            try:
                return self._resolve_once(target)
            except (TargetNotFound, FrameNotFound):
                if time.monotonic() >= deadline:
                    raise
                self.page.wait_for_timeout(150)

    def _resolve_once(self, target: Target) -> Resolved:
        frame = self._frame(target.frame)
        checks: list[StrategyCheck] = []
        chosen: tuple[int, Strategy, Locator] | None = None
        for rank, s in enumerate(target.strategies):
            loc, n = self._locate(frame, s)
            checks.append(StrategyCheck(strategy=s, matches=n))
            if n == 1 and chosen is None and loc is not None:
                chosen = (rank, s, loc)
        if chosen is None:
            raise TargetNotFound(
                f"no strategy uniquely matched {target.description!r}",
                frame=target.frame, frame_url=frame.url,
                checks=[(c.strategy.model_dump(), c.matches) for c in checks],
            )
        rank, strat, loc = chosen
        handle = loc.element_handle()
        for c in checks:
            if c.matches != 1:
                continue
            if c.strategy == strat:
                c.agrees = True
                continue
            other, _ = self._locate(frame, c.strategy)
            c.agrees = bool(other is not None and other.evaluate("(a, b) => a === b", handle))
            if not c.agrees:
                raise TargetAmbiguous(
                    f"strategies disagree for {target.description!r}",
                    chosen=strat.model_dump(), conflicting=c.strategy.model_dump(),
                )
        return Resolved(
            target=target, chosen=strat, chosen_rank=rank, checks=checks, frame_url=frame.url,
            control_text=handle.evaluate(_CONTROL_TEXT_JS) or "", handle=handle,
        )

    # ---- act ----------------------------------------------------------------------------
    def perform(self, action: Action, resolved: Resolved | None, value: str | None) -> str | None:
        result: str | None = None
        if action.kind == "navigate":
            assert action.route is not None
            self.page.goto(self.url_for(action.route))
        else:
            assert resolved is not None and resolved.handle is not None
            h = resolved.handle
            if action.kind == "click":
                h.click(timeout=self.resolve_timeout_ms)
            elif action.kind == "fill":
                h.fill(value or "", timeout=self.resolve_timeout_ms)
            elif action.kind == "select":
                h.select_option(value or "", timeout=self.resolve_timeout_ms)
            elif action.kind == "extract":
                result = h.inner_text().strip()
        self._settle()
        return result

    def _settle(self) -> None:
        """Best-effort quiescence after an action. Correctness comes from checkpoints."""
        try:
            self.page.wait_for_load_state("load", timeout=10_000)
            self.page.wait_for_load_state("networkidle", timeout=3_000)
        except Exception:
            pass

    # ---- state probes -----------------------------------------------------------------
    def text_visible(self, text: str, frame: str | None = None, regex: bool = False) -> bool:
        frames = [f for _, f in self._named_frames()] if frame == "*" else [self._frame_or_none(frame)]
        needle: str | re.Pattern[str] = re.compile(text) if regex else text
        for f in frames:
            if f is None:
                continue
            try:
                loc = f.get_by_text(needle)
                for i in range(min(loc.count(), 5)):
                    if loc.nth(i).is_visible():
                        return True
            except Exception:
                continue
        return False

    def _frame_or_none(self, name: str | None) -> Frame | None:
        try:
            return self._frame(name)
        except FrameNotFound:
            return None

    def wait_for_text(self, text: str, frame: str | None, timeout_ms: int, regex: bool = False) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if self.text_visible(text, frame, regex):
                return True
            self.page.wait_for_timeout(150)
        return False

    def screenshot(self, path: Path, mask_patterns: list[str]) -> Path:
        """Full-page screenshot with sensitive text boxed out *before* it hits disk."""
        masks = [f.get_by_text(re.compile(p)) for _, f in self._named_frames() for p in mask_patterns]
        path.parent.mkdir(parents=True, exist_ok=True)
        self.page.screenshot(path=str(path), full_page=True, mask=masks, mask_color="#000000")
        return path
