"""
Shared helpers for the PAIS GitOps tooling.

This module is imported by both ``setup_pais.py`` (apply additions/updates)
and ``cleanup_pais.py`` (apply removals). It provides:

  * Logging setup
  * YAML config loading with ``${ENV_VAR}`` interpolation (keeps secrets out
    of git - real values are injected from the environment / CI secrets)
  * Auth resolution that lets environment variables override config values
  * A thin authenticated httpx wrapper (PAISClient) with pagination helpers
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import httpx
import yaml

log = logging.getLogger("pais")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> None:
    import sys
    # Silence third-party HTTP request/connection logging (httpx & httpcore) to avoid logging IPs/endpoints
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    handler = logging.StreamHandler(sys.stdout)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[handler],
        force=True,
    )


# ---------------------------------------------------------------------------
# Config loading + ${ENV_VAR} interpolation
# ---------------------------------------------------------------------------

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_str(value: str) -> str:
    """Replace ${VAR} occurrences in a string with the matching env var."""

    def _repl(match: re.Match) -> str:
        name = match.group(1)
        env_val = os.environ.get(name)
        if env_val is None:
            log.warning("Environment variable ${%s} is not set; leaving placeholder as-is", name)
            return match.group(0)
        return env_val

    return _ENV_RE.sub(_repl, value)


def expand_env_vars(obj: Any) -> Any:
    """Recursively expand ${ENV_VAR} references in strings within a structure."""
    if isinstance(obj, dict):
        return {k: expand_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [expand_env_vars(v) for v in obj]
    if isinstance(obj, str):
        return _expand_str(obj)
    return obj


def load_config(path: str, expand: bool = True) -> dict:
    """Load a YAML config file, optionally expanding ${ENV_VAR} references."""
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    if expand:
        cfg = expand_env_vars(cfg)
    return cfg


# ---------------------------------------------------------------------------
# Auth resolution (environment overrides config)
# ---------------------------------------------------------------------------

def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Safely decode unverified JWT payload for debugging log output."""
    import base64
    try:
        parts = token.split(".")
        if len(parts) == 3:
            payload_b64 = parts[1]
            rem = len(payload_b64) % 4
            if rem > 0:
                payload_b64 += "=" * (4 - rem)
            decoded_bytes = base64.urlsafe_b64decode(payload_b64)
            return json.loads(decoded_bytes)
    except Exception:
        pass
    return None


def fetch_pais_env(base_url: str, verify_ssl: bool = True) -> dict[str, Any] | None:
    """Fetch https://<pais-host>/env.json to get PAIS OIDC configuration."""
    env_url = f"{base_url.rstrip('/')}/env.json"
    try:
        with httpx.Client(verify=verify_ssl, timeout=10) as client:
            resp = client.get(env_url)
            if resp.status_code == 200:
                data = resp.json()
                log.info("Fetched PAIS instance configuration.")
                return data
            else:
                log.debug("Fetch '%s' returned status %d", env_url, resp.status_code)
    except Exception as exc:
        log.debug("Could not fetch '%s': %s", env_url, exc)
def resolve_connection(pais_cfg: dict) -> tuple[str, dict, bool]:
    """
    Merge connection settings from config with environment variables.

    Config values (interpolated from ${ENV_VAR}) take precedence. If a setting
    is omitted or unexpanded, environment variables act as fallbacks:

      PAIS_BASE_URL, PAIS_TOKEN_URL, PAIS_CLIENT_ID, PAIS_CLIENT_SECRET,
      PAIS_SCOPE, PAIS_USERNAME, PAIS_PASSWORD, PAIS_TOKEN, PAIS_VERIFY_SSL

    Returns (base_url, auth_dict, verify_ssl).
    """
    auth_cfg = dict(pais_cfg.get("auth", {}))

    cfg_base_url = str(pais_cfg.get("base_url") or "").strip()
    if cfg_base_url and not cfg_base_url.startswith("${"):
        base_url = cfg_base_url
    else:
        base_url = os.environ.get("PAIS_BASE_URL", "").strip()

    env_overrides = {
        "token": "PAIS_TOKEN",
        "api_token": "PAIS_API_TOKEN",
        "token_url": "PAIS_TOKEN_URL",
        "client_id": "PAIS_CLIENT_ID",
        "client_secret": "PAIS_CLIENT_SECRET",
        "scope": "PAIS_SCOPE",
        "username": "PAIS_USERNAME",
        "password": "PAIS_PASSWORD",
    }
    for key, env_name in env_overrides.items():
        cfg_val = str(auth_cfg.get(key) or "").strip()
        if not cfg_val or cfg_val.startswith("${"):
            env_val = os.environ.get(env_name, "").strip()
            if env_val:
                auth_cfg[key] = env_val

    verify_ssl = auth_cfg.get("verify_ssl", True)
    if os.environ.get("PAIS_VERIFY_SSL") is not None and os.environ.get("PAIS_VERIFY_SSL", "").strip() != "":
        verify_ssl = os.environ["PAIS_VERIFY_SSL"].strip().lower() not in ("0", "false", "no")

    if not base_url:
        raise ValueError("PAIS base_url is not configured (set pais.base_url or PAIS_BASE_URL).")

    if "auth01" in base_url or "/application/o/" in base_url or "/api/v3/" in base_url:
        log.warning(
            "PAIS_BASE_URL ('%s') looks like an Identity Provider / Authentik URL. "
            "Ensure PAIS_BASE_URL points to the PAIS REST API Gateway host (e.g., https://pais.vcf05.showcase.tmm.broadcom.lab), "
            "not the Authentik login host.",
            base_url,
        )

    # Attempt fetching /env.json to auto-discover PAIS OIDC settings
    fetch_pais_env(base_url, verify_ssl=bool(verify_ssl))

    return base_url, auth_cfg, bool(verify_ssl)


class BearerAuth(httpx.Auth):
    """Static Bearer / API Token Auth for httpx."""

    def __init__(self, token: str) -> None:
        self.token = token.strip()

    def auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


class OAuth2PasswordAuth(httpx.Auth):
    """
    Custom OAuth2 Resource Owner Password Credentials Auth for httpx.
    Obtains and automatically refreshes Bearer tokens using username & password.
    Supports standard OIDC ROPC grant, Client Credentials grant, and full 6-step
    Authentik Flow Executor API App Password generation.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        username: str,
        password: str,
        client_secret: str | None = None,
        scope: str | None = None,
        verify_ssl: bool = True,
    ) -> None:
        self.token_url = (token_url or "").strip()
        self.client_id = (client_id or "").strip()
        self.username = (username or "").strip()
        self.password = (password or "").strip()
        self.client_secret = client_secret.strip() if client_secret and client_secret.strip() else None
        self.scope = scope.strip() if scope and scope.strip() else None
        self.verify_ssl = verify_ssl
        self._active_token: str | None = None
        self._token_candidates: list[str] = []

    def _generate_authentik_tokens(self, base_url: str) -> tuple[str | None, str | None]:
        """
        Programmatic Token Generation via Authentik Flow & Core API.
        Executes the Authentik authentication flow to generate:
          1. Authentik API Token (intent='api')
          2. Authentik App Password (intent='app_password')
        Returns (api_token_key, app_password_key).
        """
        import uuid
        base_authentik_url = base_url.rstrip("/")
        log.info("Executing Authentik Flow Executor & Core API to generate API Token and App Password...")

        api_token_key: str | None = None
        app_password_key: str | None = None

        with httpx.Client(verify=self.verify_ssl, follow_redirects=True, timeout=30) as session:
            try:
                # Step 1: Initialize the Authentication Flow (fetches cookies and CSRF)
                flow_url = f"{base_authentik_url}/api/v3/flows/executor/default-authentication-flow/?query=next%3D%2F"
                r1 = session.get(flow_url)
                if r1.is_error:
                    log.warning("  Authentik Step 1 (init flow) failed [%d]: %s", r1.status_code, r1.text)
                    return None, None

                csrf_token = session.cookies.get("authentik_csrf") or session.cookies.get("csrftoken") or ""
                headers = {
                    "Content-Type": "application/json",
                    "Referer": f"{base_authentik_url}/",
                }
                if csrf_token:
                    headers["X-Authentik-CSRF"] = csrf_token

                # Step 2: Submit Username
                r2 = session.post(flow_url, json={"uid_field": self.username}, headers=headers)
                if r2.is_error:
                    log.warning("  Authentik Step 2 (submit username) failed [%d]: %s", r2.status_code, r2.text)
                    return None, None

                csrf_token = session.cookies.get("authentik_csrf") or session.cookies.get("csrftoken") or csrf_token
                if csrf_token:
                    headers["X-Authentik-CSRF"] = csrf_token

                # Step 3: Submit Password
                r3 = session.post(flow_url, json={"password": self.password}, headers=headers)
                if r3.is_error:
                    log.warning("  Authentik Step 3 (submit password) failed [%d]: %s", r3.status_code, r3.text)
                    return None, None

                csrf_token = session.cookies.get("authentik_csrf") or session.cookies.get("csrftoken") or csrf_token
                if csrf_token:
                    headers["X-Authentik-CSRF"] = csrf_token

                # Step 4: Get Current Authenticated User ID (pk)
                me_url = f"{base_authentik_url}/api/v3/core/users/me/"
                r4 = session.get(me_url, headers=headers)
                if r4.is_error:
                    log.warning("  Authentik Step 4 (get user pk) failed [%d]: %s", r4.status_code, r4.text)
                    return None, None

                user_pk = r4.json().get("user", {}).get("pk")
                if not user_pk:
                    log.warning("  Authentik Step 4 failed: 'pk' not found in response: %s", r4.text)
                    return None, None

                tokens_url = f"{base_authentik_url}/api/v3/core/tokens/"

                # Step 5a: Create API Token (intent='api')
                api_tok_id = f"pais-api-tok-{uuid.uuid4().hex[:8]}"
                r5a = session.post(
                    tokens_url,
                    json={
                        "identifier": api_tok_id,
                        "intent": "api",
                        "user": user_pk,
                        "expiring": False,
                        "description": "PAIS GitOps API Token",
                    },
                    headers=headers,
                )
                if r5a.status_code in (200, 201):
                    r6a = session.get(f"{tokens_url}{api_tok_id}/view_key/", headers=headers)
                    if r6a.status_code == 200:
                        api_token_key = r6a.json().get("key")
                        log.info("  Successfully generated Authentik API Token (intent='api')!")

                # Step 5b: Create App Password Token (intent='app_password')
                app_tok_id = f"pais-app-pwd-{uuid.uuid4().hex[:8]}"
                r5b = session.post(
                    tokens_url,
                    json={
                        "identifier": app_tok_id,
                        "intent": "app_password",
                        "user": user_pk,
                        "expiring": False,
                        "description": "PAIS GitOps App Password",
                    },
                    headers=headers,
                )
                if r5b.status_code in (200, 201):
                    r6b = session.get(f"{tokens_url}{app_tok_id}/view_key/", headers=headers)
                    if r6b.status_code == 200:
                        app_password_key = r6b.json().get("key")
                        log.info("  Successfully generated Authentik App Password (intent='app_password')!")

            except Exception as exc:
                log.warning("  Authentik Flow Exception: %s", exc)

        return api_token_key, app_password_key

    def _fetch_token(self) -> str:
        req_scope = self.scope or "openid groups offline_access"
        self._token_candidates = []
        token_data: dict[str, Any] = {}
        api_token_key: str | None = None
        app_password_key: str | None = None

        with httpx.Client(verify=self.verify_ssl, timeout=30) as client:
            # --- Strategy 1: Standard OIDC ROPC Grant (grant_type=password) ---
            data1: dict[str, str] = {
                "grant_type": "password",
                "client_id": self.client_id,
                "username": self.username,
                "password": self.password,
                "scope": req_scope,
            }
            if self.client_secret:
                data1["client_secret"] = self.client_secret

            resp1 = client.post(self.token_url, data=data1)
            if resp1.status_code == 200:
                token_data = resp1.json()

            # --- Strategy 2: Client Credentials Grant (M2M) if client_secret provided ---
            if not token_data and self.client_secret:
                log.info("OIDC ROPC failed [%d]. Trying Client Credentials grant...", resp1.status_code)
                data_cc = {
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": req_scope,
                }
                resp_cc = client.post(self.token_url, data=data_cc)
                if resp_cc.status_code == 200:
                    token_data = resp_cc.json()

            # --- Strategy 3: Authentik API Token & App Password Flow ---
            if "/application/o/" in self.token_url or "/api/v3/" in self.token_url:
                base_auth_url = self.token_url.split("/application/o/")[0].split("/api/v3/")[0]
                api_token_key, app_password_key = self._generate_authentik_tokens(base_auth_url)

                # Exchange generated App Password Key at OIDC Token Endpoint if needed
                if not token_data and app_password_key:
                    log.info("Exchanging generated Authentik App Password key at OIDC token endpoint...")
                    data_app = {
                        "grant_type": "password",
                        "client_id": self.client_id,
                        "username": self.username,
                        "password": app_password_key,
                        "scope": req_scope,
                    }
                    if self.client_secret:
                        data_app["client_secret"] = self.client_secret

                    resp_app = client.post(self.token_url, data=data_app)
                    if resp_app.status_code == 200:
                        token_data = resp_app.json()

            # --- Strategy 4: Clean OIDC Form POST without scope ---
            if not token_data:
                log.info("Trying clean OIDC form payload...")
                data_clean = {
                    "grant_type": "password",
                    "client_id": self.client_id,
                    "username": self.username,
                    "password": app_password_key or self.password,
                }
                resp_clean = client.post(self.token_url, data=data_clean)
                if resp_clean.status_code == 200:
                    token_data = resp_clean.json()

            # --- Assemble token candidates in priority order ---
            # 1. OIDC access_token (JWT signed by Authentik)
            if token_data:
                access_tok = token_data.get("access_token") or token_data.get("token") or token_data.get("key")
                if access_tok and access_tok not in self._token_candidates:
                    self._token_candidates.append(access_tok)

            # 2. Authentik API Token (intent='api', format e.g. ak_...)
            if api_token_key and api_token_key not in self._token_candidates:
                self._token_candidates.append(api_token_key)

            # 3. Authentik App Password Key (intent='app_password')
            if app_password_key and app_password_key not in self._token_candidates:
                self._token_candidates.append(app_password_key)

            # 4. OIDC id_token
            if token_data:
                id_tok = token_data.get("id_token")
                if id_tok and id_tok not in self._token_candidates:
                    self._token_candidates.append(id_tok)

            if not self._token_candidates:
                log.error("Token fetch failed. Response: %s", resp1.text if 'resp1' in locals() else "No response")
                raise RuntimeError("Failed to acquire any authentication token from Authentik.")

            self._active_token = self._token_candidates[0]
            log.info("Successfully authenticated with Authentik (OIDC token acquired).")
            return self._active_token

    def auth_flow(self, request: httpx.Request):
        if not self._active_token:
            self._fetch_token()

        request.headers["Authorization"] = f"Bearer {self._active_token}"
        response = yield request

        # Only retry alternative candidates if status is 401 (invalid/rejected token)
        if response.status_code == 401 and len(self._token_candidates) > 1:
            initial_token = self._active_token
            for candidate in list(self._token_candidates):
                if candidate == initial_token:
                    continue
                log.debug("PAIS API returned 401 with active token. Retrying with candidate token...")
                self._active_token = candidate
                request.headers["Authorization"] = f"Bearer {self._active_token}"
                response = yield request
                if response.status_code < 400:
                    log.info("Token candidate succeeded with status %d!", response.status_code)
                    return

            # Restore active token to primary candidate 0 (OIDC access token)
            self._active_token = self._token_candidates[0]


def build_auth(auth_cfg: dict, verify_ssl: bool = True) -> httpx.Auth:
    """Build an auth handler (Static Bearer Token or OAuth2 Resource Owner Password) from config."""
    static_token = (
        auth_cfg.get("token") or
        auth_cfg.get("api_token") or
        os.environ.get("PAIS_TOKEN") or
        os.environ.get("PAIS_API_TOKEN")
    )
    if static_token and static_token.strip():
        log.info("Using static Bearer/API token for PAIS authentication.")
        return BearerAuth(static_token.strip())

    required = ("token_url", "client_id", "username", "password")
    missing = [k for k in required if not auth_cfg.get(k)]
    if missing:
        raise ValueError(
            f"Missing required auth settings: {', '.join(missing)}. "
            "Provide PAIS_TOKEN_URL, PAIS_CLIENT_ID, PAIS_USERNAME, PAIS_PASSWORD, or PAIS_TOKEN."
        )

    return OAuth2PasswordAuth(
        token_url=auth_cfg["token_url"],
        client_id=auth_cfg["client_id"],
        username=auth_cfg["username"],
        password=auth_cfg["password"],
        client_secret=auth_cfg.get("client_secret"),
        scope=auth_cfg.get("scope"),
        verify_ssl=verify_ssl,
    )


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------

class PAISClient:
    """Thin authenticated wrapper around httpx with PAIS list pagination."""

    def __init__(
        self,
        base_url: str,
        auth: OAuth2PasswordAuth | httpx.Auth | None = None,
        verify_ssl: bool = True,
        offline: bool = False,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._offline = offline
        headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
            "Referer": f"{self._base}/",
            "Origin": self._base,
        }
        self._client = None if offline else httpx.Client(
            auth=auth,
            headers=headers,
            verify=verify_ssl,
            timeout=60,
        )

    @classmethod
    def offline(cls, base_url: str) -> "PAISClient":
        """Construct a client that performs no network calls (for --dry-run)."""
        return cls(base_url, auth=None, offline=True)

    # -- internals ---------------------------------------------------------

    def _url(self, path: str) -> str:
        base = self._base
        # Prevent URL path duplication when base_url already includes /api/v1/control or /api/v1
        if base.endswith("/api/v1/control") and path.startswith("/api/v1/control/"):
            path = path[len("/api/v1/control"):]
        elif base.endswith("/api/v1") and path.startswith("/api/v1/"):
            path = path[len("/api/v1"):]
        elif base.endswith("/api") and path.startswith("/api/"):
            path = path[len("/api"):]

        if not path.startswith("/") and not base.endswith("/"):
            path = "/" + path

        return f"{base}{path}"

    def _ensure_online(self) -> None:
        if self._offline or self._client is None:
            raise RuntimeError("PAISClient is in offline mode; no network calls allowed.")

    @staticmethod
    def _raise_for_status(response: httpx.Response, context: str) -> None:
        if response.is_error:
            try:
                body = response.json()
            except Exception:
                body = response.text
            raise RuntimeError(
                f"{context} failed [{response.status_code}]: {json.dumps(body, indent=2, default=str)}"
            )

    # -- verbs -------------------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> Any:
        self._ensure_online()
        full_url = self._url(path)
        resp = self._client.get(full_url, **kwargs)

        if resp.is_error and "/control/" in full_url and "<html" in resp.text.lower():
            fallback_url = full_url.replace("/api/v1/control/", "/api/v1/").replace("/control/", "/")
            log.debug("GET endpoint returned %d HTML. Retrying fallback endpoint...", resp.status_code)
            resp = self._client.get(fallback_url, **kwargs)
            if resp.status_code < 400:
                return resp.json()

        self._raise_for_status(resp, f"GET {path}")
        return resp.json()

    def post(self, path: str, json_body: dict | None = None, **kwargs: Any) -> Any:
        self._ensure_online()
        full_url = self._url(path)
        resp = self._client.post(full_url, json=json_body, **kwargs)

        if resp.is_error and "/control/" in full_url and "<html" in resp.text.lower():
            fallback_url = full_url.replace("/api/v1/control/", "/api/v1/").replace("/control/", "/")
            log.debug("POST endpoint returned %d HTML. Retrying fallback endpoint...", resp.status_code)
            resp = self._client.post(fallback_url, json=json_body, **kwargs)
            if resp.status_code < 400:
                return resp.json()

        self._raise_for_status(resp, f"POST {path}")
        return resp.json()

    def patch(self, path: str, json_body: dict | None = None, **kwargs: Any) -> Any:
        self._ensure_online()
        full_url = self._url(path)
        resp = self._client.patch(full_url, json=json_body, **kwargs)

        if resp.is_error and "/control/" in full_url and "<html" in resp.text.lower():
            fallback_url = full_url.replace("/api/v1/control/", "/api/v1/").replace("/control/", "/")
            log.debug("PATCH endpoint returned %d HTML. Retrying fallback endpoint...", resp.status_code)
            resp = self._client.patch(fallback_url, json=json_body, **kwargs)
            if resp.status_code < 400:
                return resp.json()

        self._raise_for_status(resp, f"PATCH {path}")
        return resp.json()

    def put(self, path: str, json_body: dict | None = None, **kwargs: Any) -> Any:
        self._ensure_online()
        full_url = self._url(path)
        resp = self._client.put(full_url, json=json_body, **kwargs)

        if resp.is_error and "/control/" in full_url and "<html" in resp.text.lower():
            fallback_url = full_url.replace("/api/v1/control/", "/api/v1/").replace("/control/", "/")
            log.debug("PUT endpoint returned %d HTML. Retrying fallback endpoint...", resp.status_code)
            resp = self._client.put(fallback_url, json=json_body, **kwargs)
            if resp.status_code < 400:
                return resp.json()

        self._raise_for_status(resp, f"PUT {path}")
        return resp.json()

    def delete(self, path: str, **kwargs: Any) -> Any:
        self._ensure_online()
        full_url = self._url(path)
        resp = self._client.delete(full_url, **kwargs)

        if resp.is_error and "/control/" in full_url and "<html" in resp.text.lower():
            fallback_url = full_url.replace("/api/v1/control/", "/api/v1/").replace("/control/", "/")
            log.debug("DELETE endpoint returned %d HTML. Retrying fallback endpoint...", resp.status_code)
            resp = self._client.delete(fallback_url, **kwargs)
            if resp.status_code < 400:
                return resp.json()

        self._raise_for_status(resp, f"DELETE {path}")
        return resp.json()

    # -- pagination helpers ------------------------------------------------

    def list_all(self, path: str, params: dict | None = None) -> list[dict]:
        """
        Retrieve every object from a paginated list endpoint.

        Uses cursor-based pagination (``after`` + ``last_id``) which is robust
        against objects being deleted mid-iteration.
        """
        results: list[dict] = []
        base_params = dict(params or {})
        base_params.setdefault("limit", 100)
        after: str | None = None

        while True:
            page_params = dict(base_params)
            if after:
                page_params["after"] = after
            resp = self.get(path, params=page_params)
            data = resp.get("data", [])
            results.extend(data)
            if not resp.get("has_more"):
                break
            after = resp.get("last_id")
            if not after:
                break
        return results

    def find_by_name(self, path: str, name: str, params: dict | None = None) -> dict | None:
        """Return the first object from a list endpoint whose name matches."""
        matches = [obj for obj in self.list_all(path, params=params) if obj.get("name") == name]
        if len(matches) > 1:
            log.warning(
                "Found %d objects named '%s' at %s; using the first (id=%s). "
                "Names are not unique in PAIS - consider unique names for GitOps.",
                len(matches), name, path, matches[0].get("id"),
            )
        return matches[0] if matches else None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


# ---------------------------------------------------------------------------
# REX tool naming helpers
# ---------------------------------------------------------------------------

def index_id_to_hex(index_id: str) -> str:
    """Convert a UUID with hyphens to the hex form used in REX tool names."""
    return index_id.replace("-", "")


def rex_tool_name_for_index(index_id: str) -> str:
    """Built-in REX basic-search tool name for a given index id."""
    return f"search_{index_id_to_hex(index_id)}"


# ---------------------------------------------------------------------------
# API path constants (single source of truth for both scripts)
# ---------------------------------------------------------------------------

DATA_SOURCES = "/api/v1/control/data-sources"
KNOWLEDGE_BASES = "/api/v1/control/knowledge-bases"
MCP_SERVERS = "/api/v1/control/mcp-servers"
MCP_TOOLS = "/api/v1/control/mcp-servers/tools"
AGENTS = "/api/v1/compatibility/openai/v1/agents"


def kb_data_source_links(kb_id: str) -> str:
    return f"{KNOWLEDGE_BASES}/{kb_id}/data-sources"


def kb_indexes(kb_id: str) -> str:
    return f"{KNOWLEDGE_BASES}/{kb_id}/indexes"


def kb_indexings(kb_id: str, index_id: str) -> str:
    return f"{KNOWLEDGE_BASES}/{kb_id}/indexes/{index_id}/indexings"


def kb_active_indexing(kb_id: str, index_id: str) -> str:
    return f"{KNOWLEDGE_BASES}/{kb_id}/indexes/{index_id}/active-indexing"


def mcp_tool_approval(server_id: str, tool_id: str) -> str:
    return f"{MCP_SERVERS}/{server_id}/tools/{tool_id}/approval"
