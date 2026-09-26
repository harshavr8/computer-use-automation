"""Surface-neutral vocabulary shared by the agent, the artifact, and replay.

Nothing here knows about Playwright or HTML. A Target says *what* control we
mean in terms a human operator would recognize (its role, the label next to it,
its visible text, the table cell it lives in). A SurfaceDriver decides *how* to
find that on a particular surface (web DOM today; UIA/AX tree for desktop later).

Strategies are ranked. Replay uses the first one that resolves to exactly one
control, and cross-checks the others: if two strategies resolve to *different*
controls, that is ambiguity and a hard stop, never a guess.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

ControlRole = Literal["textbox", "combobox", "button", "link", "checkbox", "radio"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoleStrategy(_Strict):
    """Accessibility role + accessible name. Portable to desktop a11y trees."""
    by: Literal["role"] = "role"
    role: ControlRole
    name: str


class AnchorTextStrategy(_Strict):
    """The first control of `control` type after a visible text anchor, in reading order.

    This is how an operator finds an unlabeled field: "the box after 'Member Number:'".
    It is the workhorse for legacy screens whose inputs have no accessible name.
    """
    by: Literal["anchor_text"] = "anchor_text"
    text: str
    control: ControlRole


class TextStrategy(_Strict):
    """A control identified by its own exact visible text (links, text buttons)."""
    by: Literal["text"] = "text"
    text: str


class FieldNameStrategy(_Strict):
    """Web-only: the HTML form field name (e.g. name="q1").

    Meaningless to a human, but in server-rendered vendor apps the form contract
    is what the backend reads, so it survives tenant relabeling ("Member Number"
    vs "Account #") better than the label does. Ranked below human-meaningful
    strategies; its value is as a cross-check and a tenant-drift fallback.
    """
    by: Literal["field_name"] = "field_name"
    name: str


class TableCellStrategy(_Strict):
    """A cell addressed by row key text x column header text. For extraction."""
    by: Literal["table_cell"] = "table_cell"
    row_key: str
    column_header: str


Strategy = Annotated[
    Union[RoleStrategy, AnchorTextStrategy, TextStrategy, FieldNameStrategy, TableCellStrategy],
    Field(discriminator="by"),
]


class Target(_Strict):
    """A control on a surface: where to look (frame) and ranked ways to find it."""
    description: str = Field(description="Human-readable, e.g. 'Member Number text box'")
    frame: str | None = Field(
        default=None,
        description="Named sub-surface (iframe / child window). None = top level.",
    )
    strategies: tuple[Strategy, ...] = Field(min_length=1)


def strategy_label(s: Strategy) -> str:
    """Short human-readable form used in logs and evidence."""
    match s:
        case RoleStrategy():
            return f"role={s.role}[{s.name!r}]"
        case AnchorTextStrategy():
            return f"{s.control} after {s.text!r}"
        case TextStrategy():
            return f"text={s.text!r}"
        case FieldNameStrategy():
            return f"field_name={s.name!r}"
        case TableCellStrategy():
            return f"cell[{s.row_key!r} x {s.column_header!r}]"
    return repr(s)
