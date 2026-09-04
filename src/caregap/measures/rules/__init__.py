"""Per-measure rules. ``all_rules()`` is the registry the default engine uses.

Each rule module exposes a class implementing :class:`caregap.measures.engine.MeasureRule`
with ``measure_id``, ``rule_version``, and ``evaluate(record, ctx, value_sets) -> RuleOutput``.
Rule text/citations live in ``rules/json/<id>.json`` (built from P2's public corpus).
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from caregap.measures.engine import MeasureRule


def all_rules() -> Sequence["MeasureRule"]:
    from caregap.measures.rules.bcs import BcsRule
    from caregap.measures.rules.cbp import CbpRule
    from caregap.measures.rules.col import ColRule
    from caregap.measures.rules.eed import EedRule
    from caregap.measures.rules.screening import SnsRule, TscRule
    from caregap.measures.rules.spc import SpcRule
    from caregap.measures.rules.spd import SpdRule

    return (
        CbpRule(),
        EedRule(),
        BcsRule(),
        ColRule(),
        SpcRule(),
        SpdRule(),
        TscRule(),
        SnsRule(),
    )
