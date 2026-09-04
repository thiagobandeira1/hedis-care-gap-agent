"""``StructuredCaller``: invoke -> extract_json_object -> model_validate, ONE correction turn,
then ``AgentOutputError`` (fail closed). Exercised through ``GenericFakeChatModel``, exactly as
CI runs every agent."""

from typing import Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatResult
from pydantic import PrivateAttr

from caregap.agents.schemas import ValidationVerdict
from caregap.structured import AgentOutputError, CallTrace, StructuredCaller

CASE_KEY = "validator:p1:CBP:2025-12-31:0"
VALID = (
    '{"measure_id": "CBP", "decision": "needs_human", "exclusion_category": null, '
    '"evidence_ids": ["o1"], "rule_citation": "c14", "confidence": "low", "rationale": "ok"}'
)
GARBAGE = "I am unable to help with that request."
WRONG_SHAPE = '{"measure_id": "NOPE", "decision": "maybe"}'


class CapturingFakeModel(GenericFakeChatModel):
    """A scripted fake that also records the exact prompt of every call."""

    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    @property
    def seen(self) -> list[list[BaseMessage]]:
        return self._seen

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self._seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def fake(*outputs: str | AIMessage) -> CapturingFakeModel:
    return CapturingFakeModel(messages=iter(outputs))


def call(caller: StructuredCaller, model: CapturingFakeModel) -> ValidationVerdict:
    return caller.call(
        model,
        ValidationVerdict,
        system="You validate care gaps.",
        user=f"CASE_KEY: {CASE_KEY}\nEvidence...",
        case_key=CASE_KEY,
        model_id="fake",
    )


def test_valid_first_try_records_one_attempt() -> None:
    caller = StructuredCaller()
    model = fake(VALID)
    verdict = call(caller, model)
    assert isinstance(verdict, ValidationVerdict)
    assert (verdict.measure_id, verdict.decision, verdict.evidence_ids) == (
        "CBP",
        "needs_human",
        ["o1"],
    )
    assert verdict.verified is False  # code sets this later, never the model
    assert caller.traces == [CallTrace("ValidationVerdict", 1, "fake", CASE_KEY, None, None)]
    assert len(model.seen) == 1
    first = model.seen[0]
    assert isinstance(first[0], SystemMessage)
    assert isinstance(first[1], HumanMessage)
    assert len(first) == 2


def test_prose_and_fences_around_the_json_are_tolerated() -> None:
    caller = StructuredCaller()
    verdict = call(caller, fake(f"Sure! Here you go:\n```json\n{VALID}\n```\nDone."))
    assert verdict.decision == "needs_human"
    assert caller.traces[0].attempts == 1


def test_garbage_then_valid_takes_two_attempts_with_a_correction_turn() -> None:
    caller = StructuredCaller()
    model = fake(GARBAGE, VALID)
    verdict = call(caller, model)
    assert verdict.decision == "needs_human"
    assert len(model.seen) == 2
    assert caller.traces == [CallTrace("ValidationVerdict", 2, "fake", CASE_KEY, None, None)]
    second = model.seen[1]
    assert len(second) == 4
    assert second[:2] == model.seen[0]
    assert isinstance(second[2], AIMessage)
    assert second[2].content == GARBAGE
    correction = second[3]
    assert isinstance(correction, HumanMessage)
    assert isinstance(correction.content, str)
    assert "not valid (JudgeParseError)" in correction.content
    assert "STRICT JSON only" in correction.content
    assert "ValidationVerdict" in correction.content


def test_schema_mismatch_then_valid_names_the_validation_error() -> None:
    caller = StructuredCaller()
    model = fake(WRONG_SHAPE, VALID)
    call(caller, model)
    correction = model.seen[1][3]
    assert isinstance(correction.content, str)
    assert "not valid (ValidationError)" in correction.content


def test_garbage_twice_fails_closed_after_exactly_two_attempts() -> None:
    caller = StructuredCaller()
    model = fake(GARBAGE, GARBAGE, VALID)  # a third, valid output must never be consumed
    with pytest.raises(AgentOutputError, match="invalid output after 2 attempts") as info:
        call(caller, model)
    err = info.value
    assert (err.schema, err.error_class, err.attempts) == (
        "ValidationVerdict",
        "JudgeParseError",
        2,
    )
    assert len(model.seen) == 2
    assert caller.traces == [CallTrace("ValidationVerdict", 2, "fake", CASE_KEY, None, None)]


def test_schema_mismatch_twice_reports_validation_error_class() -> None:
    caller = StructuredCaller()
    with pytest.raises(AgentOutputError) as info:
        call(caller, fake(WRONG_SHAPE, GARBAGE))
    # The class of the LAST failure is reported.
    assert info.value.error_class == "JudgeParseError"
    with pytest.raises(AgentOutputError) as info2:
        call(caller, fake(GARBAGE, WRONG_SHAPE))
    assert info2.value.error_class == "ValidationError"
    assert [t.attempts for t in caller.traces] == [2, 2]


def test_usage_metadata_is_captured_when_present() -> None:
    caller = StructuredCaller()
    response = AIMessage(
        content=VALID,
        usage_metadata={"input_tokens": 120, "output_tokens": 40, "total_tokens": 160},
    )
    call(caller, fake(response))
    assert caller.traces[0].input_tokens == 120
    assert caller.traces[0].output_tokens == 40


def test_list_content_parts_are_joined_before_parsing() -> None:
    caller = StructuredCaller()
    head, tail = VALID[:20], VALID[20:]
    response = AIMessage(content=[{"type": "text", "text": head}, {"type": "text", "text": tail}])
    verdict = call(caller, fake(response))
    assert verdict.measure_id == "CBP"


def test_traces_accumulate_across_calls_and_default_model_id_is_unknown() -> None:
    caller = StructuredCaller()
    call(caller, fake(VALID))
    caller.call(
        fake(VALID), ValidationVerdict, system="s", user="u", case_key="validator:p2:EED:x:0"
    )
    assert [(t.case_key, t.model_id) for t in caller.traces] == [
        (CASE_KEY, "fake"),
        ("validator:p2:EED:x:0", "unknown"),
    ]
