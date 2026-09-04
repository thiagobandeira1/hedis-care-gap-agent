"""Keyless model doubles: scripted fakes, replay (strict / fallback), recording, headers."""

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from caregap.fakes import (
    CASE_KEY_HEADER,
    PROMPT_SHA_HEADER,
    RecordingChatModel,
    ReplayChatModel,
    iter_recordings,
    parse_headers,
    scripted_model,
)

CASE_KEY = "validator:p1:CBP:2025-12-31:0"
SHA = "abc123"


def prompt(case_key: str | None = CASE_KEY, sha: str | None = SHA) -> list[HumanMessage]:
    lines = []
    if case_key is not None:
        lines.append(f"{CASE_KEY_HEADER} {case_key}")
    if sha is not None:
        lines.append(f"{PROMPT_SHA_HEADER} {sha}")
    lines.append("Evidence follows.")
    return [HumanMessage(content="\n".join(lines))]


def write_recordings(path: Path, *rows: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


# --- scripted ----------------------------------------------------------------------------


def test_scripted_model_returns_outputs_in_order_then_raises_on_exhaustion() -> None:
    model = scripted_model(["one", "two"])
    assert model.invoke("x").content == "one"
    assert model.invoke("x").content == "two"
    with pytest.raises(StopIteration):
        model.invoke("x")


# --- headers -----------------------------------------------------------------------------


def test_parse_headers_reads_case_key_and_sha_from_the_human_message() -> None:
    assert parse_headers([SystemMessage(content="system"), *prompt()]) == (CASE_KEY, SHA)
    assert parse_headers(prompt(sha=None)) == (CASE_KEY, None)
    assert parse_headers(prompt(case_key=None)) == (None, SHA)
    assert parse_headers([HumanMessage(content="no headers here")]) == (None, None)
    assert parse_headers([]) == (None, None)


def test_parse_headers_only_looks_at_the_first_three_lines() -> None:
    late = HumanMessage(content=f"a\nb\nc\n{CASE_KEY_HEADER} {CASE_KEY}")
    assert parse_headers([late]) == (None, None)
    third = HumanMessage(content=f"a\nb\n{CASE_KEY_HEADER} {CASE_KEY}")
    assert parse_headers([third]) == (CASE_KEY, None)


def test_parse_headers_ignores_non_string_content() -> None:
    message = HumanMessage(content=[{"type": "text", "text": f"{CASE_KEY_HEADER} {CASE_KEY}"}])
    assert parse_headers([message]) == (None, None)


# --- replay ------------------------------------------------------------------------------


def test_replay_returns_the_recorded_response_for_the_case_key(tmp_path: Path) -> None:
    path = write_recordings(
        tmp_path / "validator.jsonl",
        {"case_key": CASE_KEY, "prompt_sha": SHA, "role": "validator", "response": "recorded"},
        {"case_key": "other", "prompt_sha": SHA, "role": "validator", "response": "nope"},
    )
    model = ReplayChatModel(recordings_path=path, mode="strict")
    assert model.invoke(prompt()).content == "recorded"
    assert (model.fallback_count, model.sha_drift_count) == (0, 0)


def test_replay_strict_miss_raises_key_error(tmp_path: Path) -> None:
    path = write_recordings(tmp_path / "validator.jsonl")
    model = ReplayChatModel(recordings_path=path, mode="strict")
    with pytest.raises(KeyError, match=CASE_KEY):
        model.invoke(prompt())
    assert model.fallback_count == 0


def test_replay_fallback_returns_sentinel_and_counts(tmp_path: Path) -> None:
    model = ReplayChatModel(
        recordings_path=tmp_path / "missing.jsonl", mode="fallback", fallback_text='{"x": 1}'
    )
    assert model.invoke(prompt()).content == '{"x": 1}'
    assert model.fallback_count == 1
    assert model.invoke(prompt(case_key="another")).content == '{"x": 1}'
    assert model.fallback_count == 2
    assert model.sha_drift_count == 0


def test_replay_default_mode_is_fallback_with_empty_object_sentinel(tmp_path: Path) -> None:
    model = ReplayChatModel(recordings_path=tmp_path / "missing.jsonl")
    assert model.mode == "fallback"
    assert model.invoke(prompt()).content == "{}"
    assert model.fallback_count == 1


def test_replay_sha_drift_is_counted_in_fallback_and_fatal_in_strict(tmp_path: Path) -> None:
    path = write_recordings(
        tmp_path / "validator.jsonl",
        {"case_key": CASE_KEY, "prompt_sha": "old", "role": "validator", "response": "recorded"},
    )
    fallback = ReplayChatModel(recordings_path=path, mode="fallback")
    assert fallback.invoke(prompt(sha="new")).content == "recorded"
    assert (fallback.sha_drift_count, fallback.fallback_count) == (1, 0)
    strict = ReplayChatModel(recordings_path=path, mode="strict")
    with pytest.raises(ValueError, match="prompt sha drift"):
        strict.invoke(prompt(sha="new"))


def test_replay_sha_check_is_skipped_when_either_side_lacks_a_sha(tmp_path: Path) -> None:
    path = write_recordings(
        tmp_path / "validator.jsonl",
        {"case_key": CASE_KEY, "prompt_sha": None, "role": "validator", "response": "a"},
        {"case_key": "k2", "prompt_sha": "s2", "role": "validator", "response": "b"},
    )
    strict = ReplayChatModel(recordings_path=path, mode="strict")
    assert strict.invoke(prompt(sha="anything")).content == "a"
    assert strict.invoke(prompt(case_key="k2", sha=None)).content == "b"
    assert strict.sha_drift_count == 0


def test_replay_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "validator.jsonl"
    row = {"case_key": CASE_KEY, "prompt_sha": SHA, "response": "r"}
    path.write_text("\n" + json.dumps(row) + "\n\n", encoding="utf-8")
    assert ReplayChatModel(recordings_path=path, mode="strict").invoke(prompt()).content == "r"


# --- recording ---------------------------------------------------------------------------


def test_recording_model_appends_rows_and_passes_responses_through(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "validator.jsonl"
    model = RecordingChatModel(
        inner=scripted_model(["first", "second"]), recordings_path=path, role="validator"
    )
    first = model.invoke(prompt())
    assert isinstance(first, AIMessage)
    assert first.content == "first"
    assert model.invoke(prompt(case_key="k2", sha=None)).content == "second"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == {
        "case_key": CASE_KEY,
        "prompt_sha": SHA,
        "response": "first",
        "role": "validator",
    }
    assert json.loads(lines[1]) == {
        "case_key": "k2",
        "prompt_sha": None,
        "response": "second",
        "role": "validator",
    }
    # Keys are sorted so recordings diff cleanly.
    assert lines[0].startswith('{"case_key"')
    assert list(iter_recordings(path)) == [json.loads(lines[0]), json.loads(lines[1])]


def test_recording_then_replay_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "drafter.jsonl"
    RecordingChatModel(inner=scripted_model(['{"gaps": []}']), recordings_path=path).invoke(
        prompt()
    )
    replay = ReplayChatModel(recordings_path=path, mode="strict")
    assert replay.invoke(prompt()).content == '{"gaps": []}'


def test_iter_recordings_on_a_missing_file_yields_nothing(tmp_path: Path) -> None:
    assert list(iter_recordings(tmp_path / "nope.jsonl")) == []
