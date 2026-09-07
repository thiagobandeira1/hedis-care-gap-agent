"""Keyless model doubles: scripted fakes for tests, replay/recording models for evals.

``ReplayChatModel`` looks up a stable ``case_key`` carried in the human message header; on a
miss it raises (strict) or returns a deterministic fallback sentinel (fallback mode) and
counts it, so published numbers can never quietly include fabricated model output.
"""

import json
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, Literal

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

CASE_KEY_HEADER = "CASE_KEY:"
PROMPT_SHA_HEADER = "PROMPT_SHA:"


def scripted_model(outputs: Sequence[str]) -> BaseChatModel:
    """Per-role scripted iterator; exhausting it mid-run raises (a test bug, by design)."""
    return GenericFakeChatModel(messages=iter(list(outputs)))


def parse_headers(messages: Sequence[BaseMessage]) -> tuple[str | None, str | None]:
    """Case key and prompt sha are carried as the first lines of the human message."""
    case_key = prompt_sha = None
    for message in messages:
        content = message.content if isinstance(message.content, str) else ""
        for line in content.splitlines()[:3]:
            if line.startswith(CASE_KEY_HEADER):
                case_key = line[len(CASE_KEY_HEADER) :].strip()
            elif line.startswith(PROMPT_SHA_HEADER):
                prompt_sha = line[len(PROMPT_SHA_HEADER) :].strip()
    return case_key, prompt_sha


class ReplayChatModel(BaseChatModel):
    """Replays recorded responses keyed by case_key from a JSONL file."""

    recordings_path: Path
    mode: Literal["strict", "fallback"] = "fallback"
    fallback_text: str = "{}"
    #: Renders the fallback from the case key (``None`` when the prompt carries none); wins
    #: over ``fallback_text`` so a sentinel can name the case's own measure.
    fallback_factory: Callable[[str | None], str] | None = None
    fallback_count: int = 0
    sha_drift_count: int = 0
    _index: dict[str, dict[str, Any]] = PrivateAttr(default_factory=dict)

    def model_post_init(self, __context: Any) -> None:
        if self.recordings_path.exists():
            for line in self.recordings_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self._index[str(row["case_key"])] = row

    @property
    def _llm_type(self) -> str:
        return "replay"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        case_key, prompt_sha = parse_headers(messages)
        row = self._index.get(case_key or "")
        if row is None:
            if self.mode == "strict":
                raise KeyError(f"no recording for case_key {case_key!r}")
            self.fallback_count += 1
            text = (
                self.fallback_factory(case_key)
                if self.fallback_factory is not None
                else self.fallback_text
            )
        else:
            if prompt_sha and row.get("prompt_sha") and row["prompt_sha"] != prompt_sha:
                if self.mode == "strict":
                    raise ValueError(f"prompt sha drift for case_key {case_key!r}")
                self.sha_drift_count += 1
            text = str(row["response"])
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


class RecordingChatModel(BaseChatModel):
    """Wraps a real model and appends ``{case_key, prompt_sha, response}`` rows to a JSONL."""

    inner: BaseChatModel
    recordings_path: Path
    role: str = "unknown"

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        response = self.inner.invoke(messages)
        text = response.content if isinstance(response.content, str) else str(response.content)
        case_key, prompt_sha = parse_headers(messages)
        self.recordings_path.parent.mkdir(parents=True, exist_ok=True)
        with self.recordings_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(
                json.dumps(
                    {
                        "case_key": case_key,
                        "prompt_sha": prompt_sha,
                        "role": self.role,
                        "response": text,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])


def iter_recordings(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)
