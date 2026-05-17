from __future__ import annotations

import json
from pathlib import Path

from kodawari.infra.io_atomic import atomic_write_canonical_json, canonical_json_text


def test_canonical_json_text_is_byte_stable() -> None:
    payload = {"z": 1, "a": {"b": True}}
    assert canonical_json_text(payload) == '{\n  "a": {\n    "b": true\n  },\n  "z": 1\n}\n'


def test_atomic_write_canonical_json_uses_trailing_newline_and_sorted_keys(tmp_path: Path) -> None:
    path = tmp_path / ".execution_recovery_decision.json"
    atomic_write_canonical_json(path, {"z": 1, "a": 2})

    assert path.read_bytes() == b'{\n  "a": 2,\n  "z": 1\n}\n'
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2, "z": 1}
