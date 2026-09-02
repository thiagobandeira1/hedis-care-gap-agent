# Build plan — hedis-care-gap-agent

Order chosen so CI is green early (engine + snapshot client + graph with fakes), then the
surfaces, then the evals, then the demo. First deferrals if time bites: SPD, the panel page,
the judged tier.

- [ ] **0. Prereqs**: P6 `v0.1.0` tag ✅, P5 `v0.1.0` tag ✅; pins in pyproject; P6 personas
      vendored (slimmed) with PROVENANCE; `scripts/scan_codes.py` → `SCAN.md`.
- [ ] **A. Foundation** (interfaces): config, logging, `p6/` (models, client Protocol + errors +
      `mask_as_of`, http, embedded, snapshot), `measures/` {ids, context, models, tri, windows,
      value_sets loader}, `structured.py`, `llm.py`, `fakes.py`.
- [ ] **B. Measure engine**: rules (cbp eed bcs col spc spd screening) + rule JSON via
      `sync_rule_text.py`, evidence/escalation/priority/packet, verdict algebra, RecordFactory
      tests, goldens on personas at 2025-12-31 + 2024-12-31.
- [ ] **C. Agents**: schemas, prompts (+shas), validator packet/verify, drafter lint,
      template drafter, tests with fakes.
- [ ] **D. Graph**: state, nodes, edges, build, hitl, runstore, outbox, runner; scenario tests
      per edge; structure + README-diagram tests; SqliteSaver round-trip.
- [ ] **E. API + CLI + UI**: FastAPI routes, RunStore wiring, `caregap` commands, Streamlit
      pages, AppTest smoke.
- [ ] **F. Evals**: gold protocol artifacts (guide, feasibility, selection, worksheets,
      labels, freeze), items/scoring/gates, engine tier + committed outcomes, pipeline replay
      (recordings when the key lands), artifacts + README regions, baseline.
- [ ] **G. Review gate**: adversarial multi-agent review; fix via PR.
- [ ] **H. Ship**: README (diagram, tables, honesty statement), ADRs 0001–0005, GIF, CI green
      (ubuntu + windows + contract), pushed + pinned.
