"""Immutable per-runtime prompt location, including installed package assets."""

from dataclasses import dataclass
from pathlib import Path
import json


@dataclass(frozen=True)
class PromptSet:
    root: Path = Path(__file__).with_name("prompts")

    def text(self, name):
        return (self.root / (name + ".txt")).read_text(encoding="utf-8")

    def json(self, name):
        return json.loads((self.root / (name + ".json")).read_text(encoding="utf-8"))
