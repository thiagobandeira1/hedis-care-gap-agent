"""Eval CODE for the care-gap agent (SPEC section 6). The ``evals/`` directory at the repo
root holds DATA only (gold labels, snapshots, recordings, artifacts, baseline); everything
importable lives here so the CLI, CI, and tests share one implementation.

Modules: ``gold`` (typed gold cases + validators + freeze hash), ``outcomes`` (one graph run
per gold patient -> per-patient outcome rows, the committed engine byte-diff fixture),
``scoring`` (SPEC section 6 semantics over ``clinevals``), ``gates`` (ratchet gates), and
``rubrics`` (the frozen outreach faithfulness judge). ``caregap.evalrun`` orchestrates them.
"""
