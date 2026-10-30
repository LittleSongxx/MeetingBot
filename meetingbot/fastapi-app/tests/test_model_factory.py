import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.model_factory import build_chat_model


def make_config():
    return SimpleNamespace(
        model_name="deepseek-flash",
        api_key="test-key",
        base_url="http://127.0.0.1:9",
        timeout_seconds=1,
    )


class ModelFactoryJsonModeTest(unittest.TestCase):
    def test_default_uses_json_schema(self):
        with patch("services.model_factory.LLM_JSON_MODE", "json_schema"):
            runnable = build_chat_model(make_config(), {"type": "object"}, "probe")
        self.assertEqual(runnable.kwargs["response_format"]["type"], "json_schema")
        self.assertTrue(runnable.kwargs["response_format"]["json_schema"]["strict"])

    def test_json_object_mode(self):
        with patch("services.model_factory.LLM_JSON_MODE", "json_object"):
            runnable = build_chat_model(make_config(), {"type": "object"}, "probe")
        self.assertEqual(runnable.kwargs["response_format"], {"type": "json_object"})


if __name__ == "__main__":
    unittest.main()
