"""Gemini LLM invocation utilities."""

from __future__ import annotations

import json
import os
import time
from enum import Enum
from importlib import import_module
from typing import Any

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
ALLOWED_ROLES = {"system", "user", "assistant"}


class LLMError(Exception):
    """Base class for all LLM invocation failures."""


class MissingAPIKeyError(LLMError):
    """Raised when GEMINI_API_KEY is missing."""


class InvalidMessagesError(LLMError):
    """Raised when messages payload is malformed."""


class UnsupportedModelError(LLMError):
    """Raised when an unknown model is requested."""


class LLMRequestError(LLMError):
    """Raised when a Gemini request fails."""


class LLMResponseFormatError(LLMError):
    """Raised when JSON mode expects valid JSON but parsing fails."""


class LLMModel(str, Enum):
    """Supported Gemini models."""

    GEMINI_2_0_FLASH = "gemini-2.0-flash"
    GEMINI_2_0_FLASH_LITE = "gemini-2.0-flash-lite"
    GEMINI_2_5_PRO = "gemini-2.5-pro"


def _normalize_model(model: LLMModel | str) -> str:
    """Normalize model input to a supported model string."""
    if isinstance(model, LLMModel):
        return model.value

    if isinstance(model, str):
        try:
            return LLMModel(model).value
        except ValueError as exc:
            raise UnsupportedModelError(f"Unsupported model: {model}") from exc

    raise UnsupportedModelError(f"Unsupported model type: {type(model).__name__}")


def _validate_messages(messages: list[dict[str, str]]) -> None:
    """Validate the OpenAI-style messages contract."""
    if not isinstance(messages, list) or not messages:
        raise InvalidMessagesError("messages must be a non-empty list")

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise InvalidMessagesError(
                f"messages[{index}] must be a dict with role/content fields"
            )

        role = message.get("role")
        content = message.get("content")

        if role not in ALLOWED_ROLES:
            raise InvalidMessagesError(
                f"messages[{index}].role must be one of {sorted(ALLOWED_ROLES)}"
            )
        if not isinstance(content, str) or not content.strip():
            raise InvalidMessagesError(
                f"messages[{index}].content must be a non-empty string"
            )


def _messages_to_prompt(messages: list[dict[str, str]]) -> str:
    """Map OpenAI-style messages into a single Gemini-friendly prompt."""
    chunks: list[str] = []
    for message in messages:
        role = message["role"].upper()
        content = message["content"].strip()
        chunks.append(f"{role}:\n{content}")
    return "\n\n".join(chunks)


def _build_client(api_key: str, timeout_seconds: float) -> Any:
    """Create a google-genai client with best-effort timeout compatibility."""
    try:
        genai = import_module("google.genai")
    except Exception as exc:
        raise LLMRequestError(
            "google-genai is required but not installed. Install `google-genai`."
        ) from exc

    timeout_ms = max(1, int(timeout_seconds * 1000))
    try:
        return genai.Client(api_key=api_key, http_options={"timeout": timeout_ms})
    except TypeError:
        return genai.Client(api_key=api_key)


def _invoke_generate_content(
    client: Any,
    model_name: str,
    prompt: str,
    generation_config: dict[str, Any],
) -> Any:
    """Invoke Gemini generate_content across minor SDK signature differences."""
    kwargs = {
        "model": model_name,
        "contents": prompt,
        "config": generation_config,
    }

    try:
        return client.models.generate_content(**kwargs)
    except TypeError:
        # Older variants use generation_config instead of config.
        kwargs.pop("config", None)
        kwargs["generation_config"] = generation_config
        return client.models.generate_content(**kwargs)


def _extract_response_text(response: Any) -> str | None:
    """Extract text payload from response across different SDK response shapes."""
    text = getattr(response, "text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    candidates = getattr(response, "candidates", None)
    if not candidates:
        return None

    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if not parts:
            continue
        collected: list[str] = []
        for part in parts:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str):
                collected.append(part_text)
        if collected:
            joined = "".join(collected).strip()
            if joined:
                return joined

    return None


def _extract_usage(response: Any) -> dict[str, int | None]:
    """Extract token usage metadata where available."""
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }

    input_tokens = getattr(usage, "prompt_token_count", None)
    if input_tokens is None:
        input_tokens = getattr(usage, "input_token_count", None)

    output_tokens = getattr(usage, "candidates_token_count", None)
    if output_tokens is None:
        output_tokens = getattr(usage, "output_token_count", None)

    total_tokens = getattr(usage, "total_token_count", None)
    if total_tokens is None:
        total_tokens = getattr(usage, "token_count", None)

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _strip_markdown_fence(text: str) -> str:
    """Remove markdown code-fence wrappers from JSON responses."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped

    lines = stripped.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _parse_json_output(text: str) -> dict[str, Any] | list[Any]:
    """Parse JSON output, tolerating markdown fenced payloads."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        fenced = _strip_markdown_fence(text)
        try:
            parsed = json.loads(fenced)
        except json.JSONDecodeError as exc:
            raise LLMResponseFormatError(
                "json_mode=True but response is not valid JSON"
            ) from exc

    if not isinstance(parsed, (dict, list)):
        raise LLMResponseFormatError(
            "json_mode=True requires response JSON to be an object or array"
        )
    return parsed


def _coerce_parsed_json(parsed: Any) -> dict[str, Any] | list[Any]:
    """Normalize SDK parsed payload into dict/list JSON values."""
    if hasattr(parsed, "model_dump"):
        payload = parsed.model_dump()
    else:
        payload = parsed

    if not isinstance(payload, (dict, list)):
        raise LLMResponseFormatError(
            "json_mode=True requires parsed response JSON to be an object or array"
        )

    return payload


def _is_transient_error(error: Exception) -> bool:
    """Best-effort classifier for retryable transport/provider errors."""
    if isinstance(error, (TimeoutError, ConnectionError)):
        return True

    message = str(error).lower()
    name = error.__class__.__name__.lower()
    transient_tokens = (
        "timeout",
        "temporarily",
        "unavailable",
        "rate limit",
        "resource exhausted",
        "connection reset",
        "deadline exceeded",
        "429",
        "500",
        "502",
        "503",
        "504",
    )
    return any(token in message or token in name for token in transient_tokens)


def invoke_llm(
    messages: list[dict[str, str]],
    model: LLMModel = LLMModel.GEMINI_2_0_FLASH,
    json_mode: bool = False,
    response_schema: Any | None = None,
    temperature: float = 0.2,
    max_output_tokens: int | None = None,
    retries: int = 1,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """Invoke Gemini with OpenAI-style messages and a unified response shape."""
    _validate_messages(messages)

    if retries < 0:
        raise LLMRequestError("retries must be greater than or equal to 0")

    api_key = os.environ.get(GEMINI_API_KEY_ENV)
    if not api_key:
        raise MissingAPIKeyError(
            f"Missing required environment variable: {GEMINI_API_KEY_ENV}"
        )

    model_name = _normalize_model(model)
    prompt = _messages_to_prompt(messages)
    client = _build_client(api_key=api_key, timeout_seconds=timeout_seconds)

    generation_config: dict[str, Any] = {"temperature": temperature}
    if max_output_tokens is not None:
        generation_config["max_output_tokens"] = max_output_tokens
    if json_mode or response_schema is not None:
        generation_config["response_mime_type"] = "application/json"
    if response_schema is not None:
        generation_config["response_schema"] = response_schema

    max_attempts = retries + 1
    attempt = 0
    last_error: Exception | None = None

    while attempt < max_attempts:
        try:
            response = _invoke_generate_content(
                client=client,
                model_name=model_name,
                prompt=prompt,
                generation_config=generation_config,
            )
            text = _extract_response_text(response)
            parsed_json: dict[str, Any] | list[Any] | None = None
            if json_mode and response_schema is not None:
                parsed = getattr(response, "parsed", None)
                if parsed is not None:
                    parsed_json = _coerce_parsed_json(parsed)

            if json_mode:
                if parsed_json is None:
                    if not text:
                        raise LLMRequestError("Gemini returned an empty response")
                    parsed_json = _parse_json_output(text)
                if not text:
                    text = json.dumps(parsed_json)
            elif not text:
                raise LLMRequestError("Gemini returned an empty response")

            return {
                "text": text,
                "json": parsed_json,
                "raw": response,
                "usage": _extract_usage(response),
            }
        except LLMResponseFormatError:
            # JSON formatting issues should fail fast and not retry.
            raise
        except Exception as exc:
            last_error = exc
            if attempt < max_attempts - 1 and _is_transient_error(exc):
                sleep_seconds = min(0.5 * (2**attempt), 2.0)
                time.sleep(sleep_seconds)
                attempt += 1
                continue
            break

    if isinstance(last_error, LLMError):
        raise last_error

    raise LLMRequestError(
        f"Gemini request failed after {max_attempts} attempt(s)"
    ) from last_error


__all__ = [
    "LLMError",
    "MissingAPIKeyError",
    "InvalidMessagesError",
    "UnsupportedModelError",
    "LLMRequestError",
    "LLMResponseFormatError",
    "LLMModel",
    "invoke_llm",
]
