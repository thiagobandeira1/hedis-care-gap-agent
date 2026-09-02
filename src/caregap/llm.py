"""Model bundles — the ONLY module importing ``langchain_anthropic``.

Per-role injection keeps CI keyless (fakes), evals reproducible (replay), and production
explicit (anthropic, key from ``.env`` only).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel

from caregap.config import ConfigError, Settings
from caregap.fakes import RecordingChatModel, ReplayChatModel, scripted_model

#: Deterministic replay fallbacks per role (fail closed, never fabricate a decision).
VALIDATOR_FALLBACK = (
    '{"measure_id": "CBP", "decision": "needs_human", "exclusion_category": null, '
    '"evidence_ids": [], "rule_citation": "recording_missing", "confidence": "low", '
    '"rationale": "No recording available for this case; routed to human review."}'
)
DRAFTER_FALLBACK = "{}"  # parse failure -> TemplateDrafter takes over (draft_error set)


@dataclass(frozen=True)
class ModelBundle:
    validator: BaseChatModel
    drafter: BaseChatModel
    model_ids: dict[str, str]
    mode: Literal["fake", "replay", "anthropic"]

    def fallback_count(self) -> int:
        total = 0
        for model in (self.validator, self.drafter):
            if isinstance(model, ReplayChatModel):
                total += model.fallback_count
        return total


def anthropic_bundle(settings: Settings, *, record_to: Path | None = None) -> ModelBundle:
    if settings.anthropic_api_key is None:
        raise ConfigError(
            "CAREGAP_ANTHROPIC_API_KEY is required for anthropic mode (set it in .env; CI is "
            "keyless and uses replay/fake models)"
        )
    from langchain_anthropic import ChatAnthropic

    key = settings.anthropic_api_key.get_secret_value()

    def build(model_id: str) -> BaseChatModel:
        return ChatAnthropic(
            model_name=model_id,
            temperature=0,
            max_retries=2,
            timeout=60,
            api_key=key,  # type: ignore[arg-type]
            stop=None,
        )

    validator: BaseChatModel = build(settings.validator_model)
    drafter: BaseChatModel = build(settings.drafter_model)
    if record_to is not None:
        validator = RecordingChatModel(
            inner=validator, recordings_path=record_to / "validator.jsonl", role="validator"
        )
        drafter = RecordingChatModel(
            inner=drafter, recordings_path=record_to / "drafter.jsonl", role="drafter"
        )
    return ModelBundle(
        validator=validator,
        drafter=drafter,
        model_ids={
            "validator_model": settings.validator_model,
            "drafter_model": settings.drafter_model,
        },
        mode="anthropic",
    )


def fake_bundle(validator_outputs: Sequence[str], drafter_outputs: Sequence[str]) -> ModelBundle:
    return ModelBundle(
        validator=scripted_model(validator_outputs),
        drafter=scripted_model(drafter_outputs),
        model_ids={"validator_model": "fake", "drafter_model": "fake"},
        mode="fake",
    )


def replay_bundle(
    recordings_dir: Path, *, mode: Literal["strict", "fallback"] = "fallback"
) -> ModelBundle:
    return ModelBundle(
        validator=ReplayChatModel(
            recordings_path=recordings_dir / "validator.jsonl",
            mode=mode,
            fallback_text=VALIDATOR_FALLBACK,
        ),
        drafter=ReplayChatModel(
            recordings_path=recordings_dir / "drafter.jsonl",
            mode=mode,
            fallback_text=DRAFTER_FALLBACK,
        ),
        model_ids={"validator_model": "replay", "drafter_model": "replay"},
        mode="replay",
    )


def bundle_for(settings: Settings) -> ModelBundle:
    if settings.models == "anthropic":
        return anthropic_bundle(settings)
    if settings.models == "replay":
        return replay_bundle(settings.recordings_dir)
    return fake_bundle([], [])
