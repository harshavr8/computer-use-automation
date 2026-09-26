"""Tool schemas the model may call. This *is* the agent's entire action space."""
from __future__ import annotations

_WHY = {"type": "string", "description": "One sentence: why this action moves toward the goal."}
_EXPECT = {"type": "string", "description": (
    "Optional. Only if you ALREADY KNOW the exact text the next screen will show. Do not guess; "
    "use arrived_text on your next turn instead.")}
_ARRIVED = {"type": "string", "description": (
    "Exact text visible on the CURRENT screen, inside a single element, that proves your previous "
    "click/navigate landed where intended. A screen title bar (e.g. 'MBR INQ - MEMBER INQUIRY') is "
    "ideal. Never a data value (balance, name). Give it the first time you see a new screen; the "
    "runtime verifies it and records it as that step's replay checkpoint.")}
_REF = {"type": "string", "description": "Control ref from the CURRENT observation, e.g. 'main:e3'."}

TOOLS: list[dict] = [
    {
        "name": "click",
        "description": "Click a control (button or link).",
        "input_schema": {"type": "object", "properties": {"ref": _REF, "why": _WHY, "expect_text": _EXPECT,
                                                           "arrived_text": _ARRIVED},
                         "required": ["ref", "why"]},
    },
    {
        "name": "fill",
        "description": ("Type into a text box. Give exactly ONE of: param (a declared input name - ALWAYS use "
                        "this when typing an input value), secret (a declared credential name), or text "
                        "(a fixed literal that is the same on every run)."),
        "input_schema": {"type": "object", "properties": {
            "ref": _REF, "why": _WHY,
            "param": {"type": "string"}, "secret": {"type": "string"}, "text": {"type": "string"},
            "expect_text": _EXPECT, "arrived_text": _ARRIVED},
            "required": ["ref", "why"]},
    },
    {
        "name": "select",
        "description": "Choose an option in a drop-down by its visible label or value. Use param when the choice is an input.",
        "input_schema": {"type": "object", "properties": {
            "ref": _REF, "why": _WHY, "option": {"type": "string"}, "param": {"type": "string"},
            "expect_text": _EXPECT, "arrived_text": _ARRIVED}, "required": ["ref", "why"]},
    },
    {
        "name": "navigate",
        "description": "Go to a route on the application, e.g. '/login'. Prefer clicking in-app links once signed on.",
        "input_schema": {"type": "object", "properties": {"route": {"type": "string"}, "why": _WHY,
                                                           "expect_text": _EXPECT, "arrived_text": _ARRIVED},
                         "required": ["route", "why"]},
    },
    {
        "name": "extract",
        "description": ("Read a value the goal asks for and return it as a named output. Address it the way an "
                        "operator would: a table cell by row text x column header, or the value next to a label."),
        "input_schema": {"type": "object", "properties": {
            "output_name": {"type": "string", "description": "snake_case, e.g. savings_balance"},
            "output_type": {"type": "string", "enum": ["string", "decimal", "integer", "date"]},
            "sensitivity": {"type": "string", "enum": ["public", "internal", "pii", "financial"]},
            "frame": {"type": "string", "description": (
                "Frame name the value is in (e.g. 'main'). If omitted, the runtime searches every frame.")},
            "by": {"type": "string", "enum": ["table_cell", "labeled_value"]},
            "row_key": {"type": "string", "description": "table_cell: exact text of a cell in the row"},
            "column_header": {"type": "string", "description": "table_cell: exact header text of the column"},
            "label": {"type": "string", "description": "labeled_value: exact label text, e.g. 'Name:'"},
            "why": _WHY, "arrived_text": _ARRIVED},
            "required": ["output_name", "output_type", "sensitivity", "by", "why"]},
    },
    {
        "name": "done",
        "description": ("Finish. result=success when the goal is achieved; result=business_outcome when the app "
                        "legitimately says it cannot be done (e.g. record not found, not authorized). evidence_text "
                        "must be exact text visible NOW, inside ONE element, that proves which screen/result you "
                        "reached: a screen title or an app message. Never a data value such as a balance or "
                        "name: those are refused because they are sensitive and differ per member."),
        "input_schema": {"type": "object", "properties": {
            "result": {"type": "string", "enum": ["success", "business_outcome"]},
            "evidence_text": {"type": "string"},
            "outcome_code": {"type": "string", "description": "business_outcome only, snake_case, e.g. member_not_found"},
            "summary": {"type": "string"}, "arrived_text": _ARRIVED},
            "required": ["result", "evidence_text", "summary"]},
    },
    {
        "name": "request_help",
        "description": ("Stop and ask a human operator. Use when blocked, when the screen is confusing or "
                        "unexpected, or when the next step would be irreversible and you were not told it is approved."),
        "input_schema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
    },
]
