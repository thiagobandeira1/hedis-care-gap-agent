"""``resolve`` is THE verdict algebra (SPEC section 2): exhaustive over Tri^3 x escalated."""

from itertools import product

import pytest

from caregap.measures.tri import Tri, Verdict, resolve

TRI: tuple[Tri, ...] = ("yes", "no", "unknown")

#: (denominator, exclusion, numerator) -> (verdict when not escalated, verdict when escalated)
TABLE: dict[tuple[Tri, Tri, Tri], tuple[Verdict, Verdict]] = {
    # denominator no -> not_eligible whatever else is known; never promoted.
    ("no", "yes", "yes"): ("not_eligible", "not_eligible"),
    ("no", "yes", "no"): ("not_eligible", "not_eligible"),
    ("no", "yes", "unknown"): ("not_eligible", "not_eligible"),
    ("no", "no", "yes"): ("not_eligible", "not_eligible"),
    ("no", "no", "no"): ("not_eligible", "not_eligible"),
    ("no", "no", "unknown"): ("not_eligible", "not_eligible"),
    ("no", "unknown", "yes"): ("not_eligible", "not_eligible"),
    ("no", "unknown", "no"): ("not_eligible", "not_eligible"),
    ("no", "unknown", "unknown"): ("not_eligible", "not_eligible"),
    # denominator unknown -> needs_review before anything else is consulted.
    ("unknown", "yes", "yes"): ("needs_review", "needs_review"),
    ("unknown", "yes", "no"): ("needs_review", "needs_review"),
    ("unknown", "yes", "unknown"): ("needs_review", "needs_review"),
    ("unknown", "no", "yes"): ("needs_review", "needs_review"),
    ("unknown", "no", "no"): ("needs_review", "needs_review"),
    ("unknown", "no", "unknown"): ("needs_review", "needs_review"),
    ("unknown", "unknown", "yes"): ("needs_review", "needs_review"),
    ("unknown", "unknown", "no"): ("needs_review", "needs_review"),
    ("unknown", "unknown", "unknown"): ("needs_review", "needs_review"),
    # eligible + definite exclusion -> excluded; never promoted.
    ("yes", "yes", "yes"): ("excluded", "excluded"),
    ("yes", "yes", "no"): ("excluded", "excluded"),
    ("yes", "yes", "unknown"): ("excluded", "excluded"),
    # eligible, not excluded: numerator decides; escalation promotes closed/gap_open.
    ("yes", "no", "yes"): ("closed", "needs_review"),
    ("yes", "no", "no"): ("gap_open", "needs_review"),
    ("yes", "no", "unknown"): ("needs_review", "needs_review"),
    # an UNKNOWN exclusion is not a definite exclusion: it falls through to the numerator.
    ("yes", "unknown", "yes"): ("closed", "needs_review"),
    ("yes", "unknown", "no"): ("gap_open", "needs_review"),
    ("yes", "unknown", "unknown"): ("needs_review", "needs_review"),
}


def test_table_covers_tri_cubed_exactly() -> None:
    assert set(TABLE) == set(product(TRI, TRI, TRI))
    assert len(TABLE) == 27


@pytest.mark.parametrize("escalated", [False, True])
@pytest.mark.parametrize(("denominator", "exclusion", "numerator"), sorted(TABLE))
def test_resolve_matches_spec_table(
    denominator: Tri, exclusion: Tri, numerator: Tri, escalated: bool
) -> None:
    expected = TABLE[(denominator, exclusion, numerator)][1 if escalated else 0]
    assert resolve(denominator, exclusion, numerator, escalated=escalated) == expected


def test_escalation_promotes_only_verdicts_it_could_flip() -> None:
    for key in TABLE:
        plain = resolve(*key, escalated=False)
        promoted = resolve(*key, escalated=True)
        if plain in {"not_eligible", "excluded"}:
            assert promoted == plain, f"{key}: {plain} must never be promoted"
        else:
            assert promoted == "needs_review", f"{key}: {plain} must promote to needs_review"


def test_every_verdict_is_reachable() -> None:
    reachable = {resolve(*key, escalated=esc) for key in TABLE for esc in (False, True)}
    assert reachable == {"not_eligible", "closed", "excluded", "gap_open", "needs_review"}


def test_precedence_denominator_then_exclusion_then_numerator() -> None:
    # A definite exclusion beats a met numerator; an ineligible patient beats an exclusion.
    assert resolve("yes", "yes", "yes", escalated=False) == "excluded"
    assert resolve("no", "yes", "yes", escalated=False) == "not_eligible"
    assert resolve("unknown", "yes", "yes", escalated=False) == "needs_review"
