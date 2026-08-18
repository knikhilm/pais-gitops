"""
OpenWebUI Integration for VCF Private AI Services (PAIS)
========================================================
Declaratively configures an existing OpenWebUI instance with PAIS OpenAI API endpoints
using credentials and settings specified under the `openwebui:` section in `config.yaml`.

Configuration Schema in config.yaml:
-------------------------------------
openwebui:
  enabled: true                           # Set to true to enable auto-configuration
  url: "${OPENWEBUI_URL}"                 # Base URL of existing OpenWebUI instance, e.g. http://10.138.218.100:3000
  api_key: "${OPENWEBUI_API_KEY}"         # OpenWebUI Admin API key or Bearer token
  username: "${OPENWEBUI_USERNAME}"       # Fallback if api_key not provided
  password: "${OPENWEBUI_PASSWORD}"       # Fallback if api_key not provided
  verify_ssl: false                       # Verify SSL certificate for OpenWebUI URL
  openai_base_url: ""                     # Optional override (defaults to tenant PAIS OpenAI URL: <pais.base_url>/api/v1/compatibility/openai/v1)
  default_model: "gpt-oss-20b-shared"     # Optional completion model to verify and select
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


def _is_enabled(val: Any) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "on")
    return False


def _resolve_val(val: Any, env_vars: list[str], default: str = "") -> str:
    s_val = str(val or "").strip()
    if s_val and not s_val.startswith("${"):
        return s_val
    for ev in env_vars:
        ev_val = os.environ.get(ev, "").strip()
        if ev_val:
            return ev_val
    return default


def get_openwebui_auth_token(owui_url: str, api_key: str, username: str, password: str, verify_ssl: bool = False) -> str:
    """Obtain OpenWebUI API Bearer token using direct API key or username/password signin."""
    if api_key:
        return api_key

    if username and password:
        log.info("Signing into OpenWebUI API at '%s' as user '%s'...", owui_url, username)
        signin_url = f"{owui_url.rstrip('/')}/api/v1/auths/signin"
        payload = {"email": username, "password": password}
        try:
            with httpx.Client(verify=verify_ssl, timeout=15) as client:
                resp = client.post(signin_url, json=payload)
                if resp.status_code == 200:
                    token = resp.json().get("token")
                    if token:
                        log.info("Successfully signed into OpenWebUI and acquired API token.")
                        return token
                log.warning("OpenWebUI signin returned status [%d]: %s", resp.status_code, resp.text)
        except Exception as exc:
            log.warning("Exception during OpenWebUI signin: %s", exc)

    return ""


def apply_openwebui_integration(cfg: dict, client: pc.PAISClient | None = None, dry_run: bool = False) -> bool:
    """
    Declaratively connects an existing OpenWebUI instance to PAIS OpenAI API compatibility endpoint
    using settings under the `openwebui:` section in `config.yaml`.
    """
    log.info("=== Step 7: OpenWebUI Integration ===")

    owui_cfg = cfg.get("openwebui", {})
    if not _is_enabled(owui_cfg.get("enabled")):
        log.info("  OpenWebUI integration is disabled or omitted in config (skip).")
        return False

    # 1. Resolve OpenWebUI Instance Connection Details
    owui_url = _resolve_val(owui_cfg.get("url"), ["OPENWEBUI_URL", "OPENWEBUI_BASE_URL"])
    if not owui_url:
        log.warning("  OpenWebUI integration enabled, but 'url' is not provided in config or OPENWEBUI_URL env var - skip.")
        return False
    owui_url = owui_url.rstrip("/")

    owui_api_key = _resolve_val(owui_cfg.get("api_key"), ["OPENWEBUI_API_KEY", "OPENWEBUI_KEY", "OPENWEBUI_TOKEN"])
    owui_username = _resolve_val(owui_cfg.get("username"), ["OPENWEBUI_USERNAME", "OPENWEBUI_USER"])
    owui_password = _resolve_val(owui_cfg.get("password"), ["OPENWEBUI_PASSWORD", "OPENWEBUI_PASS"])
    owui_verify_ssl = owui_cfg.get("verify_ssl", False)

    # 2. Resolve Target PAIS OpenAI Endpoint & Acquire PAIS Bearer JWT Token
    pais_cfg = cfg.get("pais", {})
    base_url, auth_cfg, verify_ssl = pc.resolve_connection(pais_cfg)

    custom_openai_url = _resolve_val(owui_cfg.get("openai_base_url"), ["PAIS_OPENAI_URL"])
    if custom_openai_url:
        pais_openai_url = custom_openai_url.rstrip("/")
    else:
        pais_openai_url = f"{base_url.rstrip('/')}/api/v1/compatibility/openai/v1"

    # Acquire PAIS Bearer JWT Token
    pais_jwt_token = ""
    if client and getattr(client, "auth", None):
        if isinstance(client.auth, pc.BearerAuth):
            pais_jwt_token = client.auth.token
        elif isinstance(client.auth, pc.OIDCAuth):
            try:
                pais_jwt_token = client.auth._fetch_token()
            except Exception as auth_exc:
                log.warning("  Could not fetch fresh PAIS OIDC token from active client: %s", auth_exc)

    if not pais_jwt_token:
        static_tok = _resolve_val(auth_cfg.get("token") or auth_cfg.get("api_token"), ["PAIS_TOKEN", "PAIS_API_TOKEN"])
        if static_tok:
            pais_jwt_token = static_tok
        elif auth_cfg.get("token_url"):
            auth_handler = pc.build_auth(auth_cfg, verify_ssl=verify_ssl)
            if isinstance(auth_handler, pc.OIDCAuth):
                pais_jwt_token = auth_handler._fetch_token()
            elif isinstance(auth_handler, pc.BearerAuth):
                pais_jwt_token = auth_handler.token

    if not pais_jwt_token:
        log.error("  Failed to acquire PAIS Bearer JWT Token for OpenWebUI connection.")
        return False

    log.info("  Target OpenWebUI URL : %s", owui_url)
    log.info("  Target PAIS OpenAI URL: %s", pais_openai_url)
    log.info("  Acquired PAIS JWT Token (length: %d chars)", len(pais_jwt_token))

    if dry_run:
        log.info("  [dry-run] Would configure OpenWebUI at '%s' with PAIS OpenAI endpoint '%s'", owui_url, pais_openai_url)
        return True

    # 3. Authenticate with OpenWebUI
    owui_token = get_openwebui_auth_token(
        owui_url, owui_api_key, owui_username, owui_password, verify_ssl=owui_verify_ssl
    )
    headers = {"Content-Type": "application/json"}
    if owui_token:
        headers["Authorization"] = f"Bearer {owui_token}"

    # 4. Fetch Existing OpenAI Connections from OpenWebUI & Update with PAIS Endpoint
    try:
        with httpx.Client(verify=owui_verify_ssl, timeout=20) as ow_client:
            configs_url = f"{owui_url}/api/v1/configs/openai"
            get_resp = ow_client.get(configs_url, headers=headers)

            base_urls: list[str] = []
            api_keys: list[str] = []

            if get_resp.status_code == 200:
                cur_data = get_resp.json()
                base_urls = list(cur_data.get("OPENAI_API_BASE_URLS", []) or [])
                api_keys = list(cur_data.get("OPENAI_API_KEYS", []) or [])
            else:
                log.info("  OpenWebUI GET /api/v1/configs/openai returned status [%d] - initializing new config.", get_resp.status_code)

            # Check if PAIS OpenAI URL already present in base_urls
            matched_index = -1
            for idx, u in enumerate(base_urls):
                if u.rstrip("/") == pais_openai_url.rstrip("/"):
                    matched_index = idx
                    break

            if matched_index >= 0:
                log.info("  PAIS OpenAI endpoint already exists in OpenWebUI at index %d. Updating JWT key...", matched_index)
                base_urls[matched_index] = pais_openai_url
                if matched_index < len(api_keys):
                    api_keys[matched_index] = pais_jwt_token
                else:
                    api_keys.append(pais_jwt_token)
            else:
                log.info("  Adding new PAIS OpenAI endpoint '%s' to OpenWebUI connection list...", pais_openai_url)
                base_urls.append(pais_openai_url)
                api_keys.append(pais_jwt_token)

            # Ensure matching lengths
            while len(api_keys) < len(base_urls):
                api_keys.append(pais_jwt_token)

            payload = {
                "ENABLE_OPENAI_API": True,
                "OPENAI_API_BASE_URLS": base_urls,
                "OPENAI_API_KEYS": api_keys,
            }

            post_resp = ow_client.post(configs_url, headers=headers, json=payload)
            if post_resp.status_code in (200, 201):
                log.info("  Successfully updated OpenAI API Connection in OpenWebUI at '%s'!", owui_url)
            else:
                log.warning("  OpenWebUI POST /api/v1/configs/openai returned status [%d]: %s", post_resp.status_code, post_resp.text)

            # 5. Verify Model Sync and Readiness
            target_model = owui_cfg.get("default_model") or "gpt-oss-20b-shared"
            _verify_pais_models_and_chat(pais_openai_url, pais_jwt_token, target_model, verify_ssl=verify_ssl)

            return True

    except Exception as exc:
        log.error("  Failed to configure OpenWebUI instance at '%s': %s", owui_url, exc)
        return False


def _verify_pais_models_and_chat(pais_openai_url: str, jwt_token: str, target_model: str, verify_ssl: bool = False) -> None:
    """Verify that PAIS OpenAI models endpoint lists completion models and test a chat completion."""
    models_url = f"{pais_openai_url.rstrip('/')}/models"
    headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Content-Type": "application/json",
    }

    try:
        with httpx.Client(verify=verify_ssl, timeout=30) as client:
            resp = client.get(models_url, headers=headers)
            if resp.status_code == 200:
                models_data = resp.json()
                model_ids = [m.get("id") for m in models_data.get("data", []) if m.get("id")]
                log.info("  Verified PAIS OpenAI models available (%d model(s)): %s", len(model_ids), model_ids)

                if model_ids and target_model not in model_ids:
                    target_model = model_ids[0]

                # Test Chat Completion
                chat_url = f"{pais_openai_url.rstrip('/')}/chat/completions"
                chat_payload = {
                    "model": target_model,
                    "messages": [{"role": "user", "content": "Hello! Confirm OpenWebUI connection status."}],
                    "max_tokens": 30,
                    "temperature": 0.1,
                }
                chat_resp = client.post(chat_url, headers=headers, json=chat_payload)
                if chat_resp.status_code == 200:
                    choices = chat_resp.json().get("choices", [])
                    if choices:
                        ans = choices[0].get("message", {}).get("content", "").strip()
                        log.info("  PAIS Chat Completion test successful for model '%s': '%s'", target_model, ans)
                else:
                    log.info("  PAIS Chat Completion test returned status [%d] for model '%s'", chat_resp.status_code, target_model)
            else:
                log.warning("  PAIS OpenAI models endpoint returned status [%d]", resp.status_code)
    except Exception as exc:
        log.warning("  Could not verify PAIS OpenAI chat completion: %s", exc)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Configure OpenWebUI connection to PAIS OpenAI API.")
    parser.add_argument("--config", default="tenants/pais-all-apps/config.yaml", help="Path to tenant config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making API calls")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")

    args = parser.parse_args(argv)

    pc.setup_logging(args.verbose)
    log.info("==========================================================================")
    log.info("OPENWEBUI INTEGRATION FOR VCF PRIVATE AI SERVICES (PAIS)")
    log.info("==========================================================================")

    if not os.path.exists(args.config):
        log.error("Config file '%s' not found.", args.config)
        sys.exit(1)

    cfg = pc.load_config(args.config)
    success = apply_openwebui_integration(cfg, dry_run=args.dry_run)

    if success:
        log.info("OpenWebUI integration completed successfully!")
    else:
        log.info("OpenWebUI integration was skipped or encountered warnings.")


if __name__ == "__main__":
    main()
