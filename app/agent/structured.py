"""Structured LLM output for CRM intelligence.

The configured Gemini chat model uses BaseChatModel.with_structured_output
for provider-native Pydantic output when supported.

Primary path: BaseChatModel.with_structured_output(Pydantic model), which
the configured chat model supports through LangChain.

Fallback: invoke the model and validate JSON content with Pydantic. This is
used when with_structured_output is unavailable or returns a raw message.
Natural-language splitting is not used.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
import logging
from typing import Any

from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, ValidationError

from app.agent.model import map_ai_provider_exception
from app.core.exceptions import APIError

logger = logging.getLogger(__name__)


def _text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


def _validate_payload(schema: type[BaseModel], payload: Any) -> BaseModel:
    try:
        if isinstance(payload, schema):
            return payload
        if isinstance(payload, BaseModel):
            return schema.model_validate(payload.model_dump())
        return schema.model_validate(payload)
    except ValidationError as exc:
        logger.error(
            "Structured AI output failed schema validation [schema=%s]",
            schema.__name__,
        )
        raise APIError(
            "The AI assistant returned an unusable response.",
            status_code=502,
            code="malformed_ai_output",
        ) from exc


def _parse_json_content(schema: type[BaseModel], content: Any) -> BaseModel:
    text = _text_content(content).strip()
    if not text:
        logger.error(
            "Structured AI output returned empty content [schema=%s]",
            schema.__name__,
        )
        raise APIError(
            "The AI assistant returned an unusable response.",
            status_code=502,
            code="malformed_ai_output",
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.error(
            "Structured AI output failed JSON decoding [schema=%s]",
            schema.__name__,
        )
        raise APIError(
            "The AI assistant returned an unusable response.",
            status_code=502,
            code="malformed_ai_output",
        ) from exc
    return _validate_payload(schema, payload)


def _structured_result(schema: type[BaseModel], result: Any) -> BaseModel | None:
    """Parse a with_structured_output result; None means fall back to a plain call."""
    if isinstance(result, (dict, BaseModel)):
        return _validate_payload(schema, result)
    content = getattr(result, "content", None)
    if content is not None:
        return _parse_json_content(schema, content)
    return None


@contextmanager
def _structured_output_errors(schema: type[BaseModel]) -> Iterator[None]:
    try:
        yield
    except APIError:
        raise
    except (ValidationError, OutputParserException) as exc:
        # LangChain's parsers raise OutputParserException for invalid model output. Its text
        # quotes that output, so only the error type is logged and the cause is not chained;
        # it must not reach the provider mapping, which matches quota words in exception text.
        logger.error(
            "Structured AI output failed validation [schema=%s error_type=%s]",
            schema.__name__,
            type(exc.__cause__ or exc).__name__,
        )
        raise APIError(
            "The AI assistant returned an unusable response.",
            status_code=502,
            code="malformed_ai_output",
        ) from None
    except Exception as exc:
        raise map_ai_provider_exception(
            exc,
            operation=f"structured_output:{schema.__name__}",
            default_code="llm_provider_error",
            default_message="The AI assistant could not complete this request.",
        ) from exc


def _messages(system_prompt: str, user_content: str) -> list[BaseMessage]:
    return [SystemMessage(content=system_prompt), HumanMessage(content=user_content)]


def invoke_structured_output(
    model: BaseChatModel,
    schema: type[BaseModel],
    *,
    system_prompt: str,
    user_content: str,
) -> BaseModel:
    messages = _messages(system_prompt, user_content)
    with _structured_output_errors(schema):
        if hasattr(model, "with_structured_output"):
            parsed = _structured_result(schema, model.with_structured_output(schema).invoke(messages))
            if parsed is not None:
                return parsed
        result = model.invoke(messages)
        return _parse_json_content(schema, getattr(result, "content", result))


async def ainvoke_structured_output(
    model: BaseChatModel,
    schema: type[BaseModel],
    *,
    system_prompt: str,
    user_content: str,
) -> BaseModel:
    """Async twin of invoke_structured_output; cancelling it cancels the provider request."""
    messages = _messages(system_prompt, user_content)
    with _structured_output_errors(schema):
        if hasattr(model, "with_structured_output"):
            parsed = _structured_result(
                schema, await model.with_structured_output(schema).ainvoke(messages)
            )
            if parsed is not None:
                return parsed
        result = await model.ainvoke(messages)
        return _parse_json_content(schema, getattr(result, "content", result))
