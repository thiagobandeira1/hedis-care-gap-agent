"""Three-valued logic and the single verdict-resolution function.

``resolve`` is exhaustively tested over Tri^3 x escalation presence; every measure rule
returns :class:`~caregap.measures.models.TriResult`s and lets this function produce the
verdict, so verdict semantics cannot drift between measures.
"""

from typing import Literal

Tri = Literal["yes", "no", "unknown"]
Verdict = Literal["not_eligible", "closed", "excluded", "gap_open", "needs_review"]


def resolve(denominator: Tri, exclusion: Tri, numerator: Tri, *, escalated: bool) -> Verdict:
    """The verdict algebra (SPEC section 2).

    denominator no -> not_eligible; denominator unknown -> needs_review; exclusion yes ->
    excluded; numerator yes -> closed; numerator unknown -> needs_review; else gap_open.
    Any escalation whose resolution could flip the verdict promotes gap_open/closed to
    needs_review; not_eligible/excluded are never promoted (nothing to flip toward action).
    """
    if denominator == "no":
        return "not_eligible"
    if denominator == "unknown":
        return "needs_review"
    if exclusion == "yes":
        return "excluded"
    if numerator == "unknown":
        return "needs_review"
    verdict: Verdict = "closed" if numerator == "yes" else "gap_open"
    return "needs_review" if escalated else verdict
