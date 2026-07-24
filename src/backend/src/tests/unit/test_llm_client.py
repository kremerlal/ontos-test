"""Tests for the shared LLM client factory."""
from unittest.mock import MagicMock, patch

import pytest

from src.common.llm_client import create_openai_client


def _make_settings(**overrides):
    s = MagicMock()
    s.DATABRICKS_TOKEN = overrides.get("token", "dapi-test")
    s.DATABRICKS_HOST = overrides.get("host", "https://test.databricks.com")
    s.LLM_BASE_URL = overrides.get("base_url", "")
    s.DATABRICKS_CONFIG_PROFILE = overrides.get("profile", None)
    return s


class TestCreateOpenaiClient:
    @patch.dict("os.environ", {}, clear=True)
    @patch("src.common.llm_client.OpenAI")
    def test_uses_user_token_when_provided(self, mock_openai_cls):
        settings = _make_settings(token="should-not-be-used")
        create_openai_client(settings, user_token="obo-token")

        mock_openai_cls.assert_called_once()
        assert mock_openai_cls.call_args[1]["api_key"] == "obo-token"

    @patch.dict(
        "os.environ",
        {"DATABRICKS_CLIENT_ID": "app-client", "DATABRICKS_CLIENT_SECRET": "app-secret"},
        clear=True,
    )
    @patch("src.common.llm_client._token_from_sdk", return_value="sp-token")
    @patch("src.common.llm_client.OpenAI")
    def test_prefers_app_service_principal_over_obo(self, mock_openai_cls, _mock_sdk):
        """Databricks Apps inject CLIENT_ID/SECRET; prefer that for Model Serving."""
        settings = _make_settings(token="pat-unused")
        create_openai_client(settings, user_token="obo-token")

        assert mock_openai_cls.call_args[1]["api_key"] == "sp-token"

    @patch.dict("os.environ", {}, clear=True)
    @patch("src.common.llm_client.OpenAI")
    def test_falls_back_to_settings_token(self, mock_openai_cls):
        settings = _make_settings(token="pat-token")
        create_openai_client(settings)

        assert mock_openai_cls.call_args[1]["api_key"] == "pat-token"

    @patch.dict("os.environ", {}, clear=True)
    @patch("src.common.llm_client.OpenAI")
    def test_derives_base_url_from_host(self, mock_openai_cls):
        settings = _make_settings(base_url="")
        create_openai_client(settings)

        assert mock_openai_cls.call_args[1]["base_url"] == "https://test.databricks.com/serving-endpoints"

    @patch.dict("os.environ", {}, clear=True)
    @patch("src.common.llm_client.OpenAI")
    def test_explicit_base_url_wins(self, mock_openai_cls):
        settings = _make_settings(base_url="https://custom.endpoint/v1")
        create_openai_client(settings)

        assert mock_openai_cls.call_args[1]["base_url"] == "https://custom.endpoint/v1"

    @patch.dict("os.environ", {}, clear=True)
    def test_raises_when_no_token(self):
        settings = _make_settings(token=None)
        settings.DATABRICKS_TOKEN = None

        with patch("src.common.llm_client._token_from_sdk", return_value=None):
            with pytest.raises(RuntimeError, match="No authentication token"):
                create_openai_client(settings)

    @patch.dict("os.environ", {}, clear=True)
    @patch("src.common.llm_client.OpenAI")
    def test_raises_when_no_base_url(self, mock_openai_cls):
        settings = _make_settings(base_url="")
        settings.DATABRICKS_HOST = ""

        with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
            create_openai_client(settings)
