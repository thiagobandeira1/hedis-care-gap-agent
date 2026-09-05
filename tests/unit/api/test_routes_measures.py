"""``/v1/measures``: one entry per engine measure, in registry order, with the tagged element
counts and the coverage table copied from the rule text."""

from caregap.measures.ids import ALL_MEASURES, CONFORMANCE_NOTICE, MEASURE_NAMES, STAR_IDS
from caregap.measures.rule_text import load_rule_text
from tests.unit.api.conftest import Api


def test_measures_follow_the_engine_registry(api: Api) -> None:
    response = api.client.get("/v1/measures")
    assert response.status_code == 200
    body = response.json()
    assert [m["measure_id"] for m in body] == list(ALL_MEASURES)
    for entry in body:
        text = load_rule_text(entry["measure_id"])
        assert entry["name"] == MEASURE_NAMES[entry["measure_id"]]
        assert entry["star_id"] == STAR_IDS[entry["measure_id"]]
        assert entry["rule_version"] == text.rule_version
        assert entry["conformance"] == CONFORMANCE_NOTICE
        sources = [e.source for e in text.elements]
        assert entry["element_counts"] == {
            "quoted": sources.count("quoted"),
            "demo_choice": sources.count("demo_choice"),
            "not_representable": sources.count("not_representable"),
        }
        assert sum(entry["element_counts"].values()) == len(text.elements)
        assert entry["coverage"] == text.coverage_table
        assert [e["id"] for e in entry["elements"]] == [e.id for e in text.elements]


def test_coverage_table_names_every_exclusion_element(api: Api) -> None:
    cbp = next(m for m in api.client.get("/v1/measures").json() if m["measure_id"] == "CBP")
    exclusions = [e for e in cbp["elements"] if e["kind"] == "exclusion"]
    assert exclusions
    assert set(cbp["coverage"]) == {e["id"] for e in exclusions}
    assert set(cbp["coverage"].values()) <= {"observable", "partial", "not_representable"}
    assert all(e["coverage"] == cbp["coverage"][e["id"]] for e in exclusions)
