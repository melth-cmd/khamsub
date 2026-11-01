"""STRUCTURE.md is the map; this keeps it honest.

A structure file rots the moment someone adds a module and forgets it. This
checks the one thing that actually drifts: a top-level module that no longer
appears in the map.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parent.parent


def test_every_module_is_mentioned_in_the_structure_file():
    text = (ROOT / "STRUCTURE.md").read_text(encoding="utf-8")
    modules = sorted(path.name for path in ROOT.glob("*.py"))
    missing = [name for name in modules if f"`{name}`" not in text]
    assert not missing, "STRUCTURE.md does not mention: " + ", ".join(missing)
