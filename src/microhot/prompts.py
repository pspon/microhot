"""Load the editable prompt files from ./prompts."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=8)
def load_prompt(prompts_dir: Path, name: str) -> str:
    path = Path(prompts_dir) / f"{name}.md"
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"prompt {path} is empty")
    return text
