"""Unit tests for backend.utils.invoke_llm."""

from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

invoke_llm_module = importlib.import_module("utils.invoke_llm")
InvalidMessagesError = invoke_llm_module.InvalidMessagesError
LLMModel = invoke_llm_module.LLMModel
LLMRequestError = invoke_llm_module.LLMRequestError
LLMResponseFormatError = invoke_llm_module.LLMResponseFormatError
MissingAPIKeyError = invoke_llm_module.MissingAPIKeyError
UnsupportedModelError = invoke_llm_module.UnsupportedModelError
invoke_llm = invoke_llm_module.invoke_llm


def make_response(
    text: str,
    prompt_tokens: int | None = None,
    output_tokens: int | None = None,
    total_tokens: int | None = None,
):
    """Construct a fake SDK response object."""
    response = SimpleNamespace(text=text)
    if prompt_tokens is not None or output_tokens is not None or total_tokens is not None:
        response.usage_metadata = SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=output_tokens,
            total_token_count=total_tokens,
        )
    return response


class InvokeLLMTests(unittest.TestCase):
    """Test invoke_llm validation, retries, JSON mode, and usage extraction."""

    def setUp(self) -> None:
        self.messages = [{"role": "user", "content": "Summarize this diff"}]

    def test_invalid_role_raises(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            with self.assertRaises(InvalidMessagesError):
                invoke_llm([{"role": "invalid", "content": "hello"}])

    def test_missing_content_raises(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            with self.assertRaises(InvalidMessagesError):
                invoke_llm([{"role": "user"}])  # type: ignore[list-item]

    def test_empty_messages_raises(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            with self.assertRaises(InvalidMessagesError):
                invoke_llm([])

    def test_missing_api_key_raises(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingAPIKeyError):
                invoke_llm(self.messages)

    def test_unsupported_model_raises(self) -> None:
        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            with self.assertRaises(UnsupportedModelError):
                invoke_llm(self.messages, model="gemini-not-real")  # type: ignore[arg-type]

    def test_json_mode_parses_valid_json(self) -> None:
        client = Mock()
        client.models.generate_content.return_value = make_response(
            text='{"steps":["click add","save"]}',
            prompt_tokens=12,
            output_tokens=7,
            total_tokens=19,
        )

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            with patch.object(invoke_llm_module, "_build_client", return_value=client):
                result = invoke_llm(
                    self.messages,
                    model=LLMModel.GEMINI_2_0_FLASH,
                    json_mode=True,
                )

                print(result)

        self.assertEqual(result["text"], '{"steps":["click add","save"]}')
        self.assertEqual(result["json"], {"steps": ["click add", "save"]})
        self.assertEqual(result["usage"]["input_tokens"], 12)
        self.assertEqual(result["usage"]["output_tokens"], 7)
        self.assertEqual(result["usage"]["total_tokens"], 19)

        kwargs = client.models.generate_content.call_args.kwargs
        self.assertEqual(kwargs["model"], LLMModel.GEMINI_2_0_FLASH.value)
        self.assertEqual(kwargs["config"]["response_mime_type"], "application/json")

    def test_json_mode_invalid_json_raises(self) -> None:
        client = Mock()
        client.models.generate_content.return_value = make_response("not-json-at-all")

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            with patch.object(invoke_llm_module, "_build_client", return_value=client):
                with self.assertRaises(LLMResponseFormatError):
                    invoke_llm(self.messages, json_mode=True)

    def test_retry_transient_then_success(self) -> None:
        client = Mock()
        client.models.generate_content.side_effect = [
            TimeoutError("request timeout"),
            make_response("final answer"),
        ]

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            with patch.object(invoke_llm_module, "_build_client", return_value=client):
                with patch.object(invoke_llm_module.time, "sleep", return_value=None):
                    result = invoke_llm(self.messages, retries=1)

        self.assertEqual(result["text"], "final answer")
        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_retry_exhausted_raises_request_error(self) -> None:
        client = Mock()
        client.models.generate_content.side_effect = [
            TimeoutError("request timeout"),
            TimeoutError("request timeout again"),
        ]

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            with patch.object(invoke_llm_module, "_build_client", return_value=client):
                with patch.object(invoke_llm_module.time, "sleep", return_value=None):
                    with self.assertRaises(LLMRequestError):
                        invoke_llm(self.messages, retries=1)

        self.assertEqual(client.models.generate_content.call_count, 2)

    def test_usage_metadata_absent_defaults_to_none(self) -> None:
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(text="ok")

        with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
            with patch.object(invoke_llm_module, "_build_client", return_value=client):
                result = invoke_llm(self.messages)

        self.assertEqual(result["usage"]["input_tokens"], None)
        self.assertEqual(result["usage"]["output_tokens"], None)
        self.assertEqual(result["usage"]["total_tokens"], None)


if __name__ == "__main__":
    unittest.main()
