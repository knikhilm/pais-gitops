"""
OpenWebUI Integration for VCF Private AI Services (PAIS)
========================================================
Connects OpenWebUI to PAIS OpenAI-Compatible Endpoints:
  1. Acquires a valid JWT Bearer token from Authentik OIDC / PAIS.
  2. Verifies connection to PAIS OpenAI API (e.g. https://10.138.217.12/api/v1/compatibility/openai/v1).
  3. Discovers available completion models (e.g. gpt-oss-20b-shared, gpt-oss-20b, agents).
  4. Tests a sample Chat Completion request against PAIS.
  5. Configures OpenWebUI via REST API (if running) or generates pre-configured
     `docker-compose.openwebui.yml` and `.env.openwebui` files ready for chat.

Usage:
  python setup_openwebui.py [--config tenants/pais-all-apps/config.yaml] [--openwebui-url http://localhost:3000]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import httpx

import pais_client as pc

log = logging.getLogger("pais_openwebui")


def fetch_jwt_token(cfg: dict) -> str:
    """Acquire a Bearer JWT token from Authentik/PAIS using configuration settings."""
    pais_cfg = cfg.get("pais", {})
    base_url, auth_cfg, verify_ssl = pc.resolve_connection(pais_cfg)

    # Check for direct static token in config or env
    static_token = os.environ.get("PAIS_TOKEN") or os.environ.get("PAIS_API_TOKEN") or auth_cfg.get("token")
    if static_token and static_token.strip() and not static_token.startswith("${"):
        log.info("Using explicit static Bearer token.")
        return static_token.strip()

    log.info("Authenticating with Authentik OIDC to acquire Bearer JWT token...")
    auth_handler = pc.build_auth(auth_cfg, verify_ssl=verify_ssl)

    if isinstance(auth_handler, pc.OIDCAuth):
        token = auth_handler._fetch_token()
        return token
    elif isinstance(auth_handler, pc.BearerAuth):
        return auth_handler.token
    else:
        raise RuntimeError("Unsupported authentication handler type.")


def test_pais_openai_endpoint(base_openai_url: str, jwt_token: str, verify_ssl: bool = False) -> list[str]:
    """
    Test GET /v1/models against PAIS OpenAI-compatible endpoint.
    Returns list of discovered model IDs.
    """
    norm_url = base_openai_url.rstrip("/")
    models_url = f"{norm_url}/models" if not norm_url.endswith("/models") else norm_url

    log.info("Testing PAIS OpenAI endpoint at '%s'...", models_url)

    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(verify=verify_ssl, timeout=30) as client:
            resp = client.get(models_url, headers=headers)
            if resp.status_code != 200:
                log.error("Failed to connect to PAIS OpenAI endpoint [%d]: %s", resp.status_code, resp.text)
                raise RuntimeError(f"PAIS OpenAI models request returned status {resp.status_code}")

            data = resp.json()
            models_list = data.get("data", [])
            model_ids = [m.get("id") for m in models_list if m.get("id")]

            log.info("Successfully connected to PAIS OpenAI API! Discovered %d model(s): %s", len(model_ids), model_ids)
            return model_ids
    except Exception as exc:
        log.error("Exception connecting to PAIS OpenAI endpoint '%s': %s", models_url, exc)
        raise


def test_chat_completion(base_openai_url: str, jwt_token: str, model_id: str, verify_ssl: bool = False) -> bool:
    """Send a sample chat completion request to verify PAIS model readiness."""
    norm_url = base_openai_url.rstrip("/")
    if norm_url.endswith("/models"):
        norm_url = norm_url[:-7].rstrip("/")
    chat_url = f"{norm_url}/chat/completions"

    log.info("Sending test Chat Completion to PAIS for model '%s' at '%s'...", model_id, chat_url)

    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model_id,
        "messages": [
            {"role": "user", "content": "Hello! Reply with 'PAIS OpenWebUI Integration Ready'."}
        ],
        "max_tokens": 50,
        "temperature": 0.2,
    }

    try:
        with httpx.Client(verify=verify_ssl, timeout=60) as client:
            resp = client.post(chat_url, headers=headers, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "").strip()
                    log.info("Chat Completion Successful! Response: '%s'", content)
                    return True
                else:
                    log.warning("Chat Completion returned 200 OK but no choices in response: %s", data)
                    return True
            else:
                log.warning("Chat Completion test returned status [%d]: %s", resp.status_code, resp.text)
                return False
    except Exception as exc:
        log.warning("Exception during test Chat Completion: %s", exc)
        return False


def generate_openwebui_env_and_compose(
    base_openai_url: str,
    jwt_token: str,
    model_id: str,
    out_dir: str = ".",
) -> None:
    """Generate .env.openwebui and docker-compose.openwebui.yml files for OpenWebUI."""
    env_content = f"""# OpenWebUI Pre-configured Environment for PAIS Integration
OPENAI_API_BASE_URL={base_openai_url}
OPENAI_API_KEY={jwt_token}
OPENAI_API_BASE_URLS={base_openai_url}
OPENAI_API_KEYS={jwt_token}
ENABLE_OPENAI_API=true
DEFAULT_MODELS={model_id}
WEBUI_NAME=PAIS Private AI Services
"""

    compose_content = f"""version: '3.8'

services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: pais-open-webui
    ports:
      - "3000:8080"
    environment:
      - OPENAI_API_BASE_URL={base_openai_url}
      - OPENAI_API_KEY={jwt_token}
      - OPENAI_API_BASE_URLS={base_openai_url}
      - OPENAI_API_KEYS={jwt_token}
      - ENABLE_OPENAI_API=true
      - DEFAULT_MODELS={model_id}
      - WEBUI_NAME=PAIS Private AI Services
    volumes:
      - open-webui-data:/app/backend/data
    restart: always

volumes:
  open-webui-data:
"""

    env_path = os.path.join(out_dir, ".env.openwebui")
    compose_path = os.path.join(out_dir, "docker-compose.openwebui.yml")

    with open(env_path, "w", encoding="utf-8") as f:
        f.write(env_content)
    log.info("Generated OpenWebUI environment file: %s", env_path)

    with open(compose_path, "w", encoding="utf-8") as f:
        f.write(compose_content)
    log.info("Generated OpenWebUI docker-compose file: %s", compose_path)


def configure_running_openwebui(
    openwebui_url: str,
    base_openai_url: str,
    jwt_token: str,
    model_id: str,
    api_key: str | None = None,
) -> bool:
    """Attempt to configure an already-running OpenWebUI instance via its REST API."""
    norm_url = openwebui_url.rstrip("/")
    log.info("Attempting to auto-configure running OpenWebUI instance at '%s'...", norm_url)

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        with httpx.Client(verify=False, timeout=15) as client:
            # Check if OpenWebUI is reachable
            health_resp = client.get(f"{norm_url}/api/v1/configs")
            if health_resp.status_code in (200, 401, 403):
                log.info("OpenWebUI instance is reachable at %s", norm_url)
                # Try updating OpenAI connection settings if endpoint available
                config_payload = {
                    "ENABLE_OPENAI_API": True,
                    "OPENAI_API_BASE_URLS": [base_openai_url],
                    "OPENAI_API_KEYS": [jwt_token],
                    "DEFAULT_MODELS": model_id,
                }
                cfg_resp = client.post(f"{norm_url}/api/v1/configs/openai", headers=headers, json=config_payload)
                if cfg_resp.status_code in (200, 201):
                    log.info("Successfully updated OpenAI API Connection settings in OpenWebUI!")
                    return True
                else:
                    log.info("OpenWebUI API config update returned status [%d] (manual UI setup available).", cfg_resp.status_code)
            else:
                log.info("OpenWebUI not currently running at %s (will rely on Docker startup config).", norm_url)
    except Exception as exc:
        log.info("OpenWebUI instance not directly reachable at %s (%s).", norm_url, exc)

    return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Configure OpenWebUI connection to PAIS OpenAI API.")
    parser.add_argument("--config", default="tenants/pais-all-apps/config.yaml", help="Path to tenant config.yaml")
    parser.add_argument("--openai-url", help="Override PAIS OpenAI endpoint URL (e.g. https://10.138.217.12/api/v1/compatibility/openai/v1)")
    parser.add_argument("--openwebui-url", default="http://localhost:3000", help="URL of running OpenWebUI instance")
    parser.add_argument("--jwt-token", help="Override Bearer JWT token directly")
    parser.add_argument("--model", help="Override default completion model ID")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")

    args = parser.parse_args(argv)

    pc.setup_logging(args.verbose)
    log.info("==========================================================================")
    log.info("OPENWEBUI INTEGRATION FOR VCF PRIVATE AI SERVICES (PAIS)")
    log.info("==========================================================================")

    # 1. Load tenant configuration
    cfg = pc.load_config(args.config) if os.path.exists(args.config) else {}

    # Determine PAIS OpenAI Base URL
    base_openai_url = args.openai_url
    if not base_openai_url:
        # Fallback to config or default standard IP
        base_openai_url = "https://10.138.217.12/api/v1/compatibility/openai/v1"

    log.info("Target PAIS OpenAI Endpoint: %s", base_openai_url)

    # 2. Acquire Bearer JWT Token
    jwt_token = args.jwt_token
    if not jwt_token:
        jwt_token = fetch_jwt_token(cfg)

    log.info("Acquired Bearer JWT Token (length: %d chars)", len(jwt_token))

    # 3. Test PAIS OpenAI Endpoint & Discover Models
    discovered_models = test_pais_openai_endpoint(base_openai_url, jwt_token, verify_ssl=False)

    # Select Completion Model
    selected_model = args.model
    if not selected_model:
        if "gpt-oss-20b-shared" in discovered_models:
            selected_model = "gpt-oss-20b-shared"
        elif "gpt-oss-20b" in discovered_models:
            selected_model = "gpt-oss-20b"
        elif discovered_models:
            selected_model = discovered_models[0]
        else:
            selected_model = "gpt-oss-20b-shared"

    log.info("Selected Primary Completion Model: '%s'", selected_model)

    # 4. Test Chat Completion
    test_chat_completion(base_openai_url, jwt_token, selected_model, verify_ssl=False)

    # 5. Generate Docker Compose & Env Files
    generate_openwebui_env_and_compose(base_openai_url, jwt_token, selected_model)

    # 6. Attempt Direct Configuration of Running OpenWebUI instance
    configure_running_openwebui(args.openwebui_url, base_openai_url, jwt_token, selected_model)

    log.info("")
    log.info("==========================================================================")
    log.info("OPENWEBUI SETUP READY!")
    log.info("==========================================================================")
    log.info("1. Start OpenWebUI with pre-configured PAIS connection:")
    log.info("   docker compose -f docker-compose.openwebui.yml up -d")
    log.info("")
    log.info("2. Or add connection manually in OpenWebUI UI (Settings -> Connections -> OpenAI):")
    log.info("   - API Base URL : %s", base_openai_url)
    log.info("   - API Key      : %s...", jwt_token[:20])
    log.info("   - Model        : %s", selected_model)
    log.info("==========================================================================")


if __name__ == "__main__":
    main()
