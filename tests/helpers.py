"""Helpers for loading the captured payloads in tests/fixtures/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures"


def load_text(name: str, layout: str = "reference") -> str:
    return (FIXTURES / layout / name).read_text()


def load_json(name: str, layout: str = "reference") -> Any:
    return json.loads(load_text(name, layout))
