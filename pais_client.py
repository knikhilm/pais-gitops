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
import httpx_auth
import yaml

log = logging.getLogger("pais")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
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

def resolve_connection(pais_cfg: dict) -> tuple[str, dict, bool]:
    """
    Merge connection settings from config with environment variables.

    Environment variables always take precedence so that CI can inject
    secrets without committing them to git:

      PAIS_BASE_URL, PAIS_TOKEN_URL, PAIS_CLIENT_ID,
      PAIS_SCOPE, PAIS_USERNAME, PAIS_PASSWORD, PAIS_VERIFY_SSL

    Returns (base_url, auth_dict, verify_ssl).
    """
    auth_cfg = dict(pais_cfg.get("auth", {}))

    base_url = os.environ.get("PAIS_BASE_URL", pais_cfg.get("base_url", ""))

    env_overrides = {
        "token_url": "PAIS_TOKEN_URL",
        "client_id": "PAIS_CLIENT_ID",
        "scope": "PAIS_SCOPE",
        "username": "PAIS_USERNAME",
        "password": "PAIS_PASSWORD",
    }
    for key, env_name in env_overrides.items():
        if os.environ.get(env_name) is not None:
            auth_cfg[key] = os.environ[env_name]

    verify_ssl = auth_cfg.get("verify_ssl", True)
    if os.environ.get("PAIS_VERIFY_SSL") is not None:
        verify_ssl = os.environ["PAIS_VERIFY_SSL"].strip().lower() not in ("0", "false", "no")

    if not base_url:
        raise ValueError("PAIS base_url is not configured (set pais.base_url or PAIS_BASE_URL).")

    return base_url, auth_cfg, bool(verify_ssl)


def build_auth(auth_cfg: dict, verify_ssl: bool = True) -> httpx_auth.OAuth2ResourceOwnerPasswordCredentials:
    """Build an OIDC Resource-Owner-Password auth handler from config."""
    required = ("token_url", "client_id", "username", "password")
    missing = [k for k in required if not auth_cfg.get(k)]
    if missing:
        raise ValueError(f"Missing required auth settings: {', '.join(missing)}")

    try:
        return httpx_auth.OAuth2ResourceOwnerPasswordCredentials(
            token_url=auth_cfg["token_url"],
            client_id=auth_cfg["client_id"],
            username=auth_cfg["username"],
            password=auth_cfg["password"],
            scope=auth_cfg.get("scope", "openid"),
            verify=verify_ssl,
        )
    except TypeError:
        return httpx_auth.OAuth2ResourceOwnerPasswordCredentials(
            token_url=auth_cfg["token_url"],
            client_id=auth_cfg["client_id"],
            username=auth_cfg["username"],
            password=auth_cfg["password"],
            scope=auth_cfg.get("scope", "openid"),
        )


# ---------------------------------------------------------------------------
# REST client
# ---------------------------------------------------------------------------

class PAISClient:
    """Thin authenticated wrapper around httpx with PAIS list pagination."""

    def __init__(
        self,
        base_url: str,
        auth: httpx_auth.OAuth2ResourceOwnerPasswordCredentials | None = None,
        verify_ssl: bool = True,
        offline: bool = False,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._offline = offline
        self._client = None if offline else httpx.Client(auth=auth, verify=verify_ssl, timeout=60)

    @classmethod
    def offline(cls, base_url: str) -> "PAISClient":
        """Construct a client that performs no network calls (for --dry-run)."""
        return cls(base_url, auth=None, offline=True)

    # -- internals ---------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base}{path}"

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
        resp = self._client.get(self._url(path), **kwargs)
        self._raise_for_status(resp, f"GET {path}")
        return resp.json()

    def post(self, path: str, json_body: dict | None = None, **kwargs: Any) -> Any:
        self._ensure_online()
        resp = self._client.post(self._url(path), json=json_body, **kwargs)
        self._raise_for_status(resp, f"POST {path}")
        return resp.json()

    def patch(self, path: str, json_body: dict | None = None, **kwargs: Any) -> Any:
        self._ensure_online()
        resp = self._client.patch(self._url(path), json=json_body, **kwargs)
        self._raise_for_status(resp, f"PATCH {path}")
        return resp.json()

    def delete(self, path: str, **kwargs: Any) -> Any:
        self._ensure_online()
        resp = self._client.delete(self._url(path), **kwargs)
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
