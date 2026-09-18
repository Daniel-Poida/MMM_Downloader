from __future__ import annotations

import io
import json
import unittest
import urllib.error
from email.message import Message
from unittest.mock import patch

from ytmax.ai_intent import (
    MAX_PLAN_TOKENS,
    AIIntentError,
    parse_intent_with_ai,
    verify_api_key,
)
from ytmax.settings import AI_PROVIDERS

OPENAI = AI_PROVIDERS["openai"]
OPENROUTER = AI_PROVIDERS["openrouter"]


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def _http_error(status: int, payload: dict[str, object]) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        f"{OPENAI.base_url}/models",
        status,
        "error",
        Message(),
        io.BytesIO(json.dumps(payload).encode()),
    )


class OpenAIIntentTests(unittest.TestCase):
    def test_structured_response_becomes_validated_intent(self) -> None:
        plan = {
            "topic": "интерьеры в стиле Ар-Деко",
            "urls": [],
            "count": 20,
            "min_duration_seconds": 600,
            "max_duration_seconds": None,
            "output_path": "C:\\ignored",
            "ranking": "balanced",
        }
        response = {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(plan)}],
                }
            ]
        }
        captured_request = None

        def fake_urlopen(request: object, timeout: int) -> _Response:
            nonlocal captured_request
            captured_request = request
            self.assertEqual(timeout, 30)
            return _Response(response)

        with patch("ytmax.ai_intent.urllib.request.urlopen", side_effect=fake_urlopen):
            intent = parse_intent_with_ai(
                "скачай двадцать видео про ар-деко",
                api_key="test-secret",
                output_path="D:\\Videos",
            )

        self.assertEqual(intent.topic, "интерьеры в стиле Ар-Деко")
        self.assertEqual(intent.count, 20)
        self.assertEqual(intent.min_duration_seconds, 600)
        self.assertEqual(intent.output_path, "D:\\Videos")
        assert captured_request is not None
        body = json.loads(captured_request.data.decode())
        self.assertFalse(body["store"])
        self.assertTrue(body["text"]["format"]["strict"])
        self.assertNotIn("test-secret", captured_request.data.decode())

    def test_key_is_required(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(AIIntentError):
                parse_intent_with_ai("найди видео", api_key="")

    def test_missing_output_text_is_rejected(self) -> None:
        with patch(
            "ytmax.ai_intent.urllib.request.urlopen",
            return_value=_Response({"output": []}),
        ):
            with self.assertRaises(AIIntentError):
                parse_intent_with_ai("найди видео", api_key="key")

    def test_non_youtube_url_and_boolean_count_are_rejected(self) -> None:
        invalid_plans = (
            {
                "topic": None,
                "urls": ["https://example.com/file.mp4"],
                "count": 1,
                "min_duration_seconds": None,
                "max_duration_seconds": None,
                "output_path": None,
                "ranking": "balanced",
            },
            {
                "topic": "design",
                "urls": [],
                "count": True,
                "min_duration_seconds": None,
                "max_duration_seconds": None,
                "output_path": None,
                "ranking": "balanced",
            },
        )
        for plan in invalid_plans:
            response = {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(plan)}],
                    }
                ]
            }
            with patch(
                "ytmax.ai_intent.urllib.request.urlopen",
                return_value=_Response(response),
            ):
                with self.assertRaises(AIIntentError):
                    parse_intent_with_ai("request", api_key="key")

    def test_malformed_success_payload_is_wrapped(self) -> None:
        with patch(
            "ytmax.ai_intent.urllib.request.urlopen",
            return_value=_Response(["not", "an", "object"]),  # type: ignore[arg-type]
        ):
            with self.assertRaises(AIIntentError):
                parse_intent_with_ai("request", api_key="key")


class OpenRouterIntentTests(unittest.TestCase):
    """OpenRouter говорит на диалекте chat/completions, а не Responses API."""

    def _plan_response(self, plan: dict[str, object]) -> dict[str, object]:
        return {"choices": [{"message": {"role": "assistant", "content": json.dumps(plan)}}]}

    def test_request_uses_chat_completions_with_a_strict_schema(self) -> None:
        plan = {
            "topic": "ар-деко",
            "urls": [],
            "count": 7,
            "min_duration_seconds": None,
            "max_duration_seconds": None,
            "output_path": None,
            "ranking": "views",
        }
        captured_request = None

        def fake_urlopen(request: object, timeout: int) -> _Response:
            nonlocal captured_request
            captured_request = request
            return _Response(self._plan_response(plan))

        with patch("ytmax.ai_intent.urllib.request.urlopen", side_effect=fake_urlopen):
            intent = parse_intent_with_ai(
                "скачай семь видео про ар-деко",
                api_key="sk-or-v1-test",
                provider="openrouter",
                model="openai/gpt-5.6-luna",
            )

        self.assertEqual(intent.topic, "ар-деко")
        self.assertEqual(intent.count, 7)
        self.assertEqual(intent.ranking, "views")
        assert captured_request is not None
        self.assertEqual(
            captured_request.full_url,
            f"{OPENROUTER.base_url}/chat/completions",
        )
        body = json.loads(captured_request.data.decode())
        self.assertEqual(body["model"], "openai/gpt-5.6-luna")
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["response_format"]["type"], "json_schema")
        self.assertTrue(body["response_format"]["json_schema"]["strict"])
        self.assertTrue(body["provider"]["require_parameters"])
        self.assertNotIn("text", body)
        self.assertNotIn("sk-or-v1-test", captured_request.data.decode())

    def test_output_ceiling_keeps_the_request_affordable(self) -> None:
        # Без потолка OpenRouter резервирует кредиты под максимум модели
        # (десятки тысяч токенов) и отклоняет запрос на небольшом балансе.
        captured_request = None

        def fake_urlopen(request: object, timeout: int) -> _Response:
            nonlocal captured_request
            captured_request = request
            return _Response(
                self._plan_response(
                    {
                        "topic": "метал",
                        "urls": [],
                        "count": 5,
                        "min_duration_seconds": None,
                        "max_duration_seconds": None,
                        "output_path": None,
                        "ranking": "balanced",
                    }
                )
            )

        with patch("ytmax.ai_intent.urllib.request.urlopen", side_effect=fake_urlopen):
            parse_intent_with_ai("метал", api_key="sk-or-v1-test", provider="openrouter")

        assert captured_request is not None
        body = json.loads(captured_request.data.decode())
        self.assertEqual(body["max_tokens"], MAX_PLAN_TOKENS)
        self.assertLessEqual(MAX_PLAN_TOKENS, 4096)
        # Размышления списываются из того же потолка, а для извлечения по схеме
        # они не нужны — иначе бюджет уходит в них, и план не возвращается.
        self.assertEqual(body["reasoning"]["effort"], "none")

    def test_insufficient_credits_are_reported_as_a_balance_problem(self) -> None:
        error = _http_error(
            402,
            {
                "error": {
                    "message": (
                        "This request requires more credits, or fewer max_tokens. "
                        "You requested up to 65536 tokens, but can only afford 30303."
                    )
                }
            },
        )

        with patch("ytmax.ai_intent.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(AIIntentError) as captured:
                parse_intent_with_ai("метал", api_key="sk-or-v1-test", provider="openrouter")

        self.assertEqual(captured.exception.reason, "quota")
        self.assertTrue(captured.exception.transient)
        self.assertIn("не хватает средств", str(captured.exception))

    def test_provider_default_model_is_used_when_none_given(self) -> None:
        captured_request = None

        def fake_urlopen(request: object, timeout: int) -> _Response:
            nonlocal captured_request
            captured_request = request
            return _Response(
                self._plan_response(
                    {
                        "topic": "метал",
                        "urls": [],
                        "count": 5,
                        "min_duration_seconds": None,
                        "max_duration_seconds": None,
                        "output_path": None,
                        "ranking": "balanced",
                    }
                )
            )

        with patch("ytmax.ai_intent.urllib.request.urlopen", side_effect=fake_urlopen):
            parse_intent_with_ai("метал", api_key="sk-or-v1-test", provider="openrouter")

        assert captured_request is not None
        body = json.loads(captured_request.data.decode())
        self.assertEqual(body["model"], OPENROUTER.default_model)

    def test_empty_choices_are_rejected(self) -> None:
        with patch(
            "ytmax.ai_intent.urllib.request.urlopen",
            return_value=_Response({"choices": []}),
        ):
            with self.assertRaises(AIIntentError):
                parse_intent_with_ai("метал", api_key="sk-or-v1-test", provider="openrouter")

    def test_unknown_model_is_recognised_from_the_message_alone(self) -> None:
        # OpenRouter не присылает код ошибки — только текст и статус 400.
        error = _http_error(400, {"error": {"message": "not a valid model ID"}})

        with patch("ytmax.ai_intent.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(AIIntentError) as captured:
                parse_intent_with_ai(
                    "метал",
                    api_key="sk-or-v1-test",
                    provider="openrouter",
                    model="gpt-5.6-luna",
                )

        self.assertEqual(captured.exception.reason, "model")
        self.assertIn("gpt-5.6-luna", str(captured.exception))


class VerifyApiKeyTests(unittest.TestCase):
    def test_openrouter_key_is_checked_on_its_own_endpoint(self) -> None:
        # Список моделей OpenRouter открыт всем, поэтому ключ он не проверяет.
        captured_request = None

        def fake_urlopen(request: object, timeout: int) -> _Response:
            nonlocal captured_request
            captured_request = request
            return _Response({"data": {"label": "test", "limit_remaining": 5}})

        with patch("ytmax.ai_intent.urllib.request.urlopen", side_effect=fake_urlopen):
            check = verify_api_key("sk-or-v1-test", provider="openrouter")

        self.assertTrue(check.ok)
        self.assertEqual(check.provider, "openrouter")
        self.assertIsNone(check.model_available)
        self.assertIn("OpenRouter", check.message)
        assert captured_request is not None
        self.assertEqual(captured_request.full_url, f"{OPENROUTER.base_url}/key")
        self.assertEqual(captured_request.get_method(), "GET")

    def test_rejected_openrouter_key_names_that_provider(self) -> None:
        error = _http_error(401, {"error": {"message": "No auth credentials found"}})

        with patch("ytmax.ai_intent.urllib.request.urlopen", side_effect=error):
            check = verify_api_key("sk-or-v1-test", provider="openrouter")

        self.assertFalse(check.ok)
        self.assertEqual(check.reason, "auth")
        self.assertIn("OpenRouter", check.message)
        self.assertNotIn("OpenAI", check.message)


    def test_working_key_and_model_are_confirmed(self) -> None:
        payload = {"data": [{"id": "gpt-5.6-luna"}, {"id": "gpt-5.6-sol"}]}
        captured_request = None

        def fake_urlopen(request: object, timeout: int) -> _Response:
            nonlocal captured_request
            captured_request = request
            return _Response(payload)

        with patch("ytmax.ai_intent.urllib.request.urlopen", side_effect=fake_urlopen):
            check = verify_api_key("sk-test-key-0000-1111", model="gpt-5.6-luna")

        self.assertTrue(check.ok)
        self.assertTrue(check.model_available)
        self.assertEqual(check.reason, "ok")
        assert captured_request is not None
        # Проверка не должна ничего стоить: это GET без тела запроса.
        self.assertEqual(captured_request.get_method(), "GET")
        self.assertIsNone(captured_request.data)
        self.assertEqual(captured_request.full_url, f"{OPENAI.base_url}/models")

    def test_unknown_model_does_not_blame_the_key(self) -> None:
        payload = {"data": [{"id": "gpt-5.6-luna"}]}

        with patch(
            "ytmax.ai_intent.urllib.request.urlopen",
            return_value=_Response(payload),
        ):
            check = verify_api_key("sk-test-key-0000-1111", model="gpt-5.4-mini")

        self.assertTrue(check.ok)
        self.assertFalse(check.model_available)
        self.assertEqual(check.reason, "model")
        self.assertIn("gpt-5.4-mini", check.message)

    def test_rejected_key_is_final_and_explains_itself(self) -> None:
        error = _http_error(401, {"error": {"message": "Incorrect API key provided"}})

        with patch("ytmax.ai_intent.urllib.request.urlopen", side_effect=error):
            check = verify_api_key("sk-test-key-0000-1111", model="gpt-5.6-luna")

        self.assertFalse(check.ok)
        self.assertEqual(check.reason, "auth")
        self.assertFalse(check.transient)
        self.assertIn("401", check.message)

    def test_network_failure_stays_retryable(self) -> None:
        with patch(
            "ytmax.ai_intent.urllib.request.urlopen",
            side_effect=urllib.error.URLError("dns"),
        ):
            check = verify_api_key("sk-test-key-0000-1111", model="gpt-5.6-luna")

        self.assertFalse(check.ok)
        self.assertEqual(check.reason, "network")
        self.assertTrue(check.transient)

    def test_empty_key_is_reported_before_any_request(self) -> None:
        with patch("ytmax.ai_intent.urllib.request.urlopen") as urlopen:
            check = verify_api_key("   ")

        urlopen.assert_not_called()
        self.assertFalse(check.ok)
        self.assertEqual(check.reason, "empty")


class ErrorClassificationTests(unittest.TestCase):
    def test_unknown_model_is_reported_as_a_model_problem(self) -> None:
        error = _http_error(
            400,
            {"error": {"message": "The model does not exist", "code": "model_not_found"}},
        )

        with patch("ytmax.ai_intent.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(AIIntentError) as captured:
                parse_intent_with_ai("найди видео", api_key="k", model="gpt-5.4-mini")

        self.assertEqual(captured.exception.reason, "model")
        self.assertIn("gpt-5.4-mini", str(captured.exception))

    def test_rate_limit_is_transient_but_auth_failure_is_not(self) -> None:
        cases = (
            (429, "quota", True),
            (500, "server", True),
            (401, "auth", False),
            (403, "permission", False),
        )
        for status, reason, transient in cases:
            with self.subTest(status=status):
                error = _http_error(status, {"error": {"message": "nope"}})
                with patch(
                    "ytmax.ai_intent.urllib.request.urlopen",
                    side_effect=error,
                ):
                    with self.assertRaises(AIIntentError) as captured:
                        parse_intent_with_ai("найди видео", api_key="k")

                self.assertEqual(captured.exception.reason, reason)
                self.assertEqual(captured.exception.transient, transient)


if __name__ == "__main__":
    unittest.main()
