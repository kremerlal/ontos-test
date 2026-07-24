"""Shared factory for creating OpenAI clients pointed at Databricks serving endpoints.

Every LLM consumer in the codebase (LLMService, LLMSearchManager,
OntologyGeneratorManager) should use ``create_openai_client`` instead of
duplicating the authentication + base-URL logic.
"""

import os
from typing import Optional, Tuple

from openai import OpenAI

from src.common.config import Settings
from src.common.logging import get_logger

logger = get_logger(__name__)


def _token_from_sdk(*, profile: Optional[str] = None) -> Optional[str]:
    """Resolve a Bearer token via Databricks SDK unified auth."""
    try:
        from databricks.sdk.core import Config

        config_kwargs = {}
        if profile:
            # pydantic doesn't push .env values to os.environ; pass profile explicitly
            config_kwargs["profile"] = profile
        config = Config(**config_kwargs)
        headers = config.authenticate()
        if headers and "Authorization" in headers:
            auth_header = headers["Authorization"]
            if auth_header.startswith("Bearer "):
                return auth_header[7:]
    except Exception as sdk_err:
        logger.debug("Could not get token from SDK config: %s", sdk_err)
    return None


def _resolve_token(
    settings: Settings,
    *,
    user_token: Optional[str] = None,
) -> Tuple[str, str]:
    """Return ``(token, source_label)`` for Model Serving auth.

    Resolution order:
      1. App service principal (``DATABRICKS_CLIENT_ID`` / ``_SECRET``) — preferred
         in Databricks Apps. The app SP receives ``CAN_QUERY`` via the bound
         serving-endpoint resource. OBO user tokens often lack the
         ``model-serving`` scope even when ``serving.serving-endpoints`` is in
         the manifest (consent / bundle-deploy wipe issues).
      2. ``user_token`` (OBO / per-request token)
      3. ``settings.DATABRICKS_TOKEN`` or ``DATABRICKS_TOKEN`` env (local PAT)
      4. Databricks SDK default config (``~/.databrickscfg`` / profile)
    """
    if os.environ.get("DATABRICKS_CLIENT_ID") and os.environ.get("DATABRICKS_CLIENT_SECRET"):
        token = _token_from_sdk()
        if token:
            return token, "app_service_principal"

    if user_token:
        return user_token, "user_token"

    token = settings.DATABRICKS_TOKEN or os.environ.get("DATABRICKS_TOKEN")
    if token:
        return token, "settings/env"

    token = _token_from_sdk(profile=settings.DATABRICKS_CONFIG_PROFILE)
    if token:
        label = (
            f"sdk_profile={settings.DATABRICKS_CONFIG_PROFILE}"
            if settings.DATABRICKS_CONFIG_PROFILE
            else "sdk_default"
        )
        return token, label

    raise RuntimeError(
        "No authentication token available. "
        "Pass a user_token, set DATABRICKS_TOKEN, or configure the Databricks SDK."
    )


def create_openai_client(
    settings: Settings,
    *,
    user_token: Optional[str] = None,
):
    """Create an OpenAI client authenticated against a Databricks serving endpoint.

    See ``_resolve_token`` for authentication precedence. In Databricks Apps,
    the app service principal is preferred over the OBO user token so Model
    Serving works without requiring the user token to carry ``model-serving``.

    Args:
        settings: Application settings carrying host/token/LLM config.
        user_token: Per-user OBO token from the ``x-forwarded-access-token``
            request header. Used when app SP credentials are unavailable.

    Returns:
        A configured ``openai.OpenAI`` client instance.

    Raises:
        RuntimeError: If no token can be resolved or no base URL is available.
    """
    token, token_source = _resolve_token(settings, user_token=user_token)

    base_url = settings.LLM_BASE_URL
    if not base_url and settings.DATABRICKS_HOST:
        host = settings.DATABRICKS_HOST.rstrip("/")
        if not host.startswith("http://") and not host.startswith("https://"):
            host = f"https://{host}"
        base_url = f"{host}/serving-endpoints"

    if not base_url:
        raise RuntimeError(
            "LLM_BASE_URL not configured and cannot be derived from DATABRICKS_HOST."
        )

    logger.info("Creating OpenAI client — base_url=%s, auth=%s", base_url, token_source)

    return OpenAI(api_key=token, base_url=base_url)
