"""Structured, redacted run log (JSONL) plus an evidence folder per run."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..safety.redact import Redactor


def new_run_id(prefix: str) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{ts}-{uuid.uuid4().hex[:6]}"


class RunLog:
    """Every write goes through the redactor. There is no un-redacted write path."""

    def __init__(self, root: Path, run_id: str, redactor: Redactor) -> None:
        self.run_id = run_id
        self.dir = Path(root) / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.redactor = redactor
        self._seq = 0
        self._fh = (self.dir / "events.jsonl").open("a", encoding="utf-8")

    def event(self, kind: str, **data: Any) -> dict[str, Any]:
        self._seq += 1
        record = {
            "seq": self._seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "kind": kind,
            **self.redactor.obj(data),
        }
        self._fh.write(json.dumps(record, default=str) + "\n")
        self._fh.flush()
        return record

    def path(self, name: str) -> Path:
        return self.dir / name

    def write_json(self, name: str, data: Any) -> Path:
        p = self.path(name)
        p.write_text(json.dumps(self.redactor.obj(data), indent=2, default=str), encoding="utf-8")
        return p

    def close(self) -> None:
        self._fh.close()
