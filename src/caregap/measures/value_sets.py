"""Value-set loading for the engine.

Files under ``caregap/measures/value_sets/*.json`` are ``{"id", "code_system", "source",
"codes": [{"code", "display"?, "intensity"?}], "note"?}``; ``source`` is allowlisted and
every code must appear in the committed scan (``SCAN.md``) or be marked ``untested_by_data``.
P6's own sets are vendored into ``p6_vendored.json`` (parity-tested against the installed
P6 package).
"""

import json
from functools import lru_cache
from importlib import resources
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ValueSetSource = Literal["p6-valuesets-2026.08", "synthea-scan", "public-acc-aha", "public-cms"]
StatinIntensity = Literal["low", "moderate", "high"]


class ValueSetCode(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    display: str | None = None
    intensity: StatinIntensity | None = None
    untested_by_data: bool = False


class ValueSet(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    code_system: str
    source: ValueSetSource
    codes: list[ValueSetCode] = Field(default_factory=list)
    note: str | None = None

    @property
    def code_set(self) -> frozenset[str]:
        return frozenset(c.code for c in self.codes)

    def intensity_of(self, code: str) -> StatinIntensity | None:
        for c in self.codes:
            if c.code == code:
                return c.intensity
        return None


class ValueSets(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    sets: dict[str, ValueSet]

    def __getitem__(self, set_id: str) -> ValueSet:
        try:
            return self.sets[set_id]
        except KeyError as exc:
            raise KeyError(f"unknown value set {set_id!r}") from exc

    def codes(self, set_id: str) -> frozenset[str]:
        return self[set_id].code_set

    def system(self, set_id: str) -> str:
        return self[set_id].code_system


@lru_cache(maxsize=1)
def load_value_sets() -> ValueSets:
    root = resources.files("caregap.measures") / "value_sets"
    sets: dict[str, ValueSet] = {}
    version = "unversioned"
    for entry in sorted(root.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".json"):
            continue
        raw = json.loads(entry.read_text(encoding="utf-8"))
        if entry.name == "p6_vendored.json":
            version = str(raw.get("version", version))
            for vs_raw in raw.get("sets", []):
                vs = ValueSet.model_validate(vs_raw)
                sets[vs.id] = vs
            continue
        vs = ValueSet.model_validate(raw)
        if vs.id in sets:
            raise ValueError(f"duplicate value set id {vs.id!r} in {entry.name}")
        sets[vs.id] = vs
    return ValueSets(version=version, sets=sets)
