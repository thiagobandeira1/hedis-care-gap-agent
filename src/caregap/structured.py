"""``StructuredCaller`` — the ONE structured-output path shared by real, fake, replay, and
recording models: invoke -> ``clinevals.extract_json_object`` -> ``model_validate``, exactly
one correction turn on parse/validation failure, then :class:`AgentOutputError` (fail closed).
``GenericFakeChatModel`` cannot do ``with_structured_output``, so this is the only path CI can
exercise end-to-end; using it everywhere means CI tests the true parser and retry branch.
"""

from dataclasses import dataclass, field
from typing import Any, TypeVar

from clinevals import JudgeParseError, extract_json_object
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class AgentOutputError(RuntimeError):
    """The model failed to produce a valid structured output after one correction turn."""

    def __init__(self, schema: str, error_class: str, attempts: int) -> None:
        super().__init__(f"{schema}: invalid output after {attempts} attempts ({error_class})")
        self.schema = schema
        self.error_class = error_class
        self.attempts = attempts


@dataclass(frozen=True)
class CallTrace:
    schema: str
    attempts: int
    model_id: str
    case_key: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class StructuredCaller:
    traces: list[CallTrace] = field(default_factory=list)

    def call(
        self,
        model: BaseChatModel,
        schema: type[T],
        *,
        system: str,
        user: str,
        case_key: str,
        model_id: str = "unknown",
    ) -> T:
        messages: list[BaseMessage] = [SystemMessage(content=system), HumanMessage(content=user)]
        last_error = "unknown"
        usage: tuple[int | None, int | None] = (None, None)
        for attempt in (1, 2):
            response = model.invoke(messages)
            text = _text_of(response)
            usage = _usage_of(response)
            try:
                parsed = schema.model_validate(extract_json_object(text))
            except (JudgeParseError, ValidationError) as exc:
                last_error = type(exc).__name__
                if attempt == 2:
                    break
                # One correction turn: the assistant's text plus the error, never a third try.
                messages = [
                    *messages,
                    AIMessage(content=text),
                    HumanMessage(
                        content=(
                            f"Your previous output was not valid ({last_error}). Reply with STRICT "
                            f"JSON only, matching the {schema.__name__} schema exactly."
                        )
                    ),
                ]
                continue
            self.traces.append(
                CallTrace(schema.__name__, attempt, model_id, case_key, usage[0], usage[1])
            )
            return parsed
        self.traces.append(CallTrace(schema.__name__, 2, model_id, case_key, usage[0], usage[1]))
        raise AgentOutputError(schema.__name__, last_error, 2)


def _text_of(response: BaseMessage) -> str:
    content = response.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict) and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "".join(parts)


def _usage_of(response: BaseMessage) -> tuple[int | None, int | None]:
    usage: Any = getattr(response, "usage_metadata", None)
    if not isinstance(usage, dict):
        return (None, None)
    return (usage.get("input_tokens"), usage.get("output_tokens"))
