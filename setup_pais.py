"""
VCF Private AI Services (PAIS) - GitOps Apply Script
====================================================
Reads a YAML configuration file and reconciles the *desired* additions and
updates into a PAIS instance. The script is **idempotent**: re-running it
against an unchanged config performs no creates, so it is safe to run on
every push in a GitOps pipeline.

Provisioning order:

  0. Kubernetes CRDs (ModelEndpoints & InferenceGatewayRoutes)
  1. Data Sources   (created if missing; optional connectivity test)
  2. Knowledge Bases + linked Data Sources
  3. Knowledge Base Indexes  (optional indexing trigger on first creation)
  4. MCP Servers    (external; created if missing)
  5. MCP Tool approval (approves the tools named in config)
  6. REX tool discovery  (built-in search tools auto-created per index)
  7. Agents          (created if missing, otherwise updated in place)

Removals are handled separately by ``cleanup_pais.py``.

Usage
-----
  python setup_pais.py --config config.yaml [--dry-run] [--verbose]

Dependencies
------------
  pip install httpx httpx-auth pyyaml
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from typing import Any

import k8s_manager as km
import pais_client as pc
import setup_openwebui
from pais_client import log


# ---------------------------------------------------------------------------
# 0. Kubernetes Model Endpoints & Inference Gateway Routes
# ---------------------------------------------------------------------------

def apply_kubernetes_resources(config: dict, dry_run: bool, config_path: str = "config.yaml", manifests_out_dir: str = "k8s-manifests") -> list[dict]:
    """
    Generate and apply Kubernetes Custom Resources for ModelEndpoints and InferenceGatewayRoutes.
    """
    log.info("=== Step 0: Kubernetes ModelEndpoints & InferenceGatewayRoutes ===")
    manifests = km.generate_k8s_manifests(config)

    if manifests:
        norm_path = config_path.replace("\\", "/").strip("/")
        parts = norm_path.split("/")
        if len(parts) >= 2 and parts[-1] in ("config.yaml", "config.yml"):
            tenant_name = parts[-2]
            filename = f"{tenant_name}-pais-resources.yaml"
        else:
            filename = "pais-resources.yaml"

        out_file = os.path.join(manifests_out_dir, filename)
        km.write_manifests_file(manifests, out_file)
        km.apply_k8s_manifests(config, manifests, dry_run=dry_run)
        km.wait_for_model_endpoints(config, dry_run=dry_run)
    else:
        log.info("  No Kubernetes ModelEndpoints or InferenceGatewayRoutes configured in config.")

    return manifests


# ---------------------------------------------------------------------------
# Helper: Universal Resource Updater & Sanitization Checks
# ---------------------------------------------------------------------------

def _is_masked(val: Any) -> bool:
    """Return True if val is None, empty, or contains asterisks / 'masked' indicating secret sanitization by API."""
    if val is None:
        return True
    if isinstance(val, str):
        s = val.strip()
        return not s or "*" in s or s.lower() == "masked"
    if isinstance(val, dict):
        if not val:
            return True
        return any(_is_masked(v) for v in val.values())
    return False


def _normalize_url(url: Any) -> str:
    if not url:
        return ""
    return str(url).strip().rstrip("/")


def _options_differ(existing_options: Any, cfg_options: Any) -> bool:
    if not cfg_options and not existing_options:
        return False
    if not isinstance(cfg_options, dict):
        return False
    if not isinstance(existing_options, dict):
        cfg_non_empty = {k: v for k, v in cfg_options.items() if v is not None and str(v).strip() != ""}
        return bool(cfg_non_empty)

    for k, v in cfg_options.items():
        v_str = str(v).strip() if v is not None else ""
        if not v_str:
            continue
        exist_v = existing_options.get(k)
        exist_v_str = str(exist_v).strip() if exist_v is not None else ""
        if v_str != exist_v_str:
            return True
    return False


def _unlink_and_delete_data_source(client: pc.PAISClient, ds_id: str, ds_name: str) -> list[str]:
    """
    Unlink a data source from all Knowledge Bases referencing it, then delete the data source.
    Returns a list of kb_ids that were unlinked so they can be re-linked to the new data source.
    """
    unlinked_kb_ids: list[str] = []

    try:
        all_kbs = client.list_all(pc.KNOWLEDGE_BASES)
        for kb in all_kbs:
            kb_id = kb.get("id")
            if not kb_id:
                continue
            try:
                links = client.list_all(pc.kb_data_source_links(kb_id))
                for link in links:
                    linked_ds_id = (link.get("data_source") or {}).get("id") or link.get("data_source_id")
                    if linked_ds_id == ds_id:
                        link_id = link.get("id")
                        if link_id:
                            client.delete(f"{pc.kb_data_source_links(kb_id)}/{link_id}")
                            log.info("  [%s] Unlinked old data source from KB '%s' (kb_id=%s)", ds_name, kb.get("name", kb_id), kb_id)
                            if kb_id not in unlinked_kb_ids:
                                unlinked_kb_ids.append(kb_id)
            except Exception as link_exc:
                log.warning("  [%s] Could not inspect/unlink data source from KB '%s': %s", ds_name, kb_id, link_exc)
    except Exception as kb_exc:
        log.warning("  [%s] Could not list Knowledge Bases for unlinking: %s", ds_name, kb_exc)

    client.delete(f"{pc.DATA_SOURCES}/{ds_id}")
    log.info("  [%s] Deleted old data source (id=%s)", ds_name, ds_id)

    return unlinked_kb_ids


def update_resource(client: pc.PAISClient, path: str, json_body: dict[str, Any]) -> dict[str, Any]:
    """
    Attempt to update an existing resource trying PATCH first, then POST, then PUT.
    Accommodates different HTTP verb preferences across PAIS REST API endpoints.
    """
    errors: list[str] = []

    try:
        return client.patch(path, json_body=json_body)
    except Exception as exc:
        errors.append(f"PATCH: {exc}")

    try:
        return client.post(path, json_body=json_body)
    except Exception as exc:
        errors.append(f"POST: {exc}")

    try:
        return client.put(path, json_body=json_body)
    except Exception as exc:
        errors.append(f"PUT: {exc}")

    raise RuntimeError(f"Failed to update resource at '{path}' via PATCH/POST/PUT. Errors: {'; '.join(errors)}")


# ---------------------------------------------------------------------------
# 1. Data Sources
# ---------------------------------------------------------------------------

def apply_data_sources(client: pc.PAISClient, ds_configs: list[dict], dry_run: bool) -> dict[str, str]:
    """Create data sources that don't yet exist. Returns {name -> id}."""
    log.info("=== Step 1: Data Sources ===")
    name_to_id: dict[str, str] = {}

    for ds in ds_configs:
        name = ds["name"]
        ds_type = ds["type"]
        origin_url = ds["origin_url"]
        credentials = ds.get("credentials", "")
        description = ds.get("description", "")
        test_conn = ds.get("test_connection", False)

        if isinstance(credentials, str) and credentials.strip().startswith("{"):
            try:
                credentials = json.loads(credentials.strip())
            except Exception:
                credentials = credentials.strip()

        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "type": ds_type,
            "origin_url": origin_url,
            "credentials": credentials,
        }
        if ds.get("options"):
            payload["options"] = ds["options"]

        if dry_run:
            log.info("  [dry-run] ensure data source '%s' (type=%s)", name, ds_type)
            name_to_id[name] = f"dry-run-ds-{name}"
            continue

        existing = client.find_by_name(pc.DATA_SOURCES, name)
        if existing:
            ds_id = existing["id"]

            # Data sources in PAIS only permit updating description via PATCH.
            # Changes to immutable parameters (type, origin_url, options)
            # require unlinking, deleting, recreating, and relinking the data source.
            immutable_changed = False

            if existing.get("type") and str(existing.get("type")).strip().upper() != str(ds_type or "").strip().upper():
                immutable_changed = True
            if existing.get("origin_url") and _normalize_url(existing.get("origin_url")) != _normalize_url(origin_url):
                immutable_changed = True
            if ds.get("options") and _options_differ(existing.get("options"), ds.get("options")):
                immutable_changed = True

            description_changed = ((existing.get("description") or "").strip() != (description or "").strip())

            if immutable_changed:
                log.info("  [%s] Immutable parameter changed (type, origin_url, or options). Unlinking, deleting, and recreating data source...", name)
                unlinked_kb_ids = _unlink_and_delete_data_source(client, ds_id, name)

                if test_conn:
                    log.info("  [%s] Testing connectivity...", name)
                    test_payload = {"origin_url": origin_url, "type": ds_type, "credentials": credentials}
                    if ds.get("options"):
                        test_payload["options"] = ds["options"]
                    result = client.post(f"{pc.DATA_SOURCES}/test-connection", json_body=test_payload)
                    if result.get("status") != "CONNECTIVITY_RESULT_SUCCESS":
                        raise RuntimeError(f"Connectivity test for data source '{name}' failed: {result}")
                    log.info("  [%s] Connectivity OK", name)

                resp = client.post(pc.DATA_SOURCES, json_body=payload)
                new_ds_id = resp["id"]
                name_to_id[name] = new_ds_id
                log.info("  [%s] Recreated data source -> id=%s", name, new_ds_id)

                for kb_id in unlinked_kb_ids:
                    try:
                        client.post(pc.kb_data_source_links(kb_id), json_body={"data_source_id": new_ds_id})
                        log.info("  [%s] Relinked new data source (id=%s) to KB (kb_id=%s)", name, new_ds_id, kb_id)
                    except Exception as relink_exc:
                        log.warning("  [%s] Failed to relink new data source to KB (kb_id=%s): %s", name, kb_id, relink_exc)

            elif description_changed:
                log.info("  [%s] Updating description...", name)
                resp = client.patch(f"{pc.DATA_SOURCES}/{ds_id}", json_body={"description": description})
                name_to_id[name] = ds_id
                log.info("  Updated data source '%s' description -> id=%s", name, ds_id)

            else:
                name_to_id[name] = ds_id
                log.info("  Data source '%s' already exists -> id=%s (up to date)", name, ds_id)

            continue

        if test_conn:
            log.info("  [%s] Testing connectivity...", name)
            test_payload = {"origin_url": origin_url, "type": ds_type, "credentials": credentials}
            if ds.get("options"):
                test_payload["options"] = ds["options"]
            result = client.post(
                f"{pc.DATA_SOURCES}/test-connection",
                json_body=test_payload,
            )
            if result.get("status") != "CONNECTIVITY_RESULT_SUCCESS":
                raise RuntimeError(f"Connectivity test for data source '{name}' failed: {result}")
            log.info("  [%s] Connectivity OK", name)

        resp = client.post(pc.DATA_SOURCES, json_body=payload)
        name_to_id[name] = resp["id"]
        log.info("  Created data source '%s' -> id=%s", name, resp["id"])

    return name_to_id


# ---------------------------------------------------------------------------
# 2 & 3. Knowledge Bases + Indexes
# ---------------------------------------------------------------------------

def apply_knowledge_bases(
    client: pc.PAISClient,
    kb_configs: list[dict],
    ds_name_to_id: dict[str, str],
    dry_run: bool,
) -> dict[str, dict[str, str]]:
    """Create KBs/indexes and link data sources idempotently.

    Returns {kb_name -> {"kb_id": ..., "index_id": ...}}.
    """
    log.info("=== Step 2: Knowledge Bases ===")
    kb_info: dict[str, dict[str, str]] = {}

    for kb in kb_configs:
        kb_name = kb["name"]
        kb_payload = {
            "name": kb_name,
            "description": kb.get("description", ""),
            "data_origin_type": kb.get("data_origin_type", "DATA_SOURCES"),
            "index_refresh_policy": kb.get("index_refresh_policy", {"policy_type": "MANUAL"}),
        }

        if dry_run:
            log.info("  [dry-run] ensure knowledge base '%s'", kb_name)
            kb_id = f"dry-run-kb-{kb_name}"
            for ds_name in kb.get("data_sources", []):
                log.info("  [dry-run]   link data source '%s'", ds_name)
            idx_cfg = kb.get("index", {})
            kb_info[kb_name] = {"kb_id": kb_id, "index_id": f"dry-run-idx-{kb_name}" if idx_cfg else ""}
            continue

        existing_kb = client.find_by_name(pc.KNOWLEDGE_BASES, kb_name)
        if existing_kb:
            kb_id = existing_kb["id"]
            needs_update = (
                existing_kb.get("description", "") != kb_payload["description"]
                or existing_kb.get("data_origin_type") != kb_payload["data_origin_type"]
                or existing_kb.get("index_refresh_policy") != kb_payload["index_refresh_policy"]
            )
            if needs_update:
                resp = update_resource(client, f"{pc.KNOWLEDGE_BASES}/{kb_id}", kb_payload)
                log.info("  Updated knowledge base '%s' -> id=%s", kb_name, kb_id)
            else:
                log.info("  Knowledge base '%s' already exists -> id=%s (up to date)", kb_name, kb_id)
        else:
            resp = client.post(pc.KNOWLEDGE_BASES, json_body=kb_payload)
            kb_id = resp["id"]
            log.info("  Created knowledge base '%s' -> id=%s", kb_name, kb_id)

        _link_data_sources(client, kb_name, kb_id, kb.get("data_sources", []), ds_name_to_id)

        index_id = _apply_index(client, kb_name, kb_id, kb.get("index", {}))
        kb_info[kb_name] = {"kb_id": kb_id, "index_id": index_id}

    return kb_info


def _link_data_sources(
    client: pc.PAISClient,
    kb_name: str,
    kb_id: str,
    ds_names: list[str],
    ds_name_to_id: dict[str, str],
) -> None:
    existing_links = client.list_all(pc.kb_data_source_links(kb_id))
    linked_ds_ids = {
        (link.get("data_source") or {}).get("id")
        for link in existing_links
    }

    for ds_name in ds_names:
        ds_id = ds_name_to_id.get(ds_name)
        if not ds_id:
            raise ValueError(
                f"Knowledge base '{kb_name}' references data source '{ds_name}' "
                "which is not defined in the data_sources section."
            )
        if ds_id in linked_ds_ids:
            log.info("  Data source '%s' already linked to KB '%s' (skip)", ds_name, kb_name)
            continue
        client.post(pc.kb_data_source_links(kb_id), json_body={"data_source_id": ds_id})
        log.info("  Linked data source '%s' -> KB '%s'", ds_name, kb_name)


def _apply_index(client: pc.PAISClient, kb_name: str, kb_id: str, idx_cfg: dict) -> str:
    if not idx_cfg:
        log.warning("  No index defined for KB '%s' - skipping index creation", kb_name)
        return ""

    idx_name = idx_cfg.get("name", f"{kb_name}-index")
    existing_indexes = client.list_all(pc.kb_indexes(kb_id))
    existing_index = next((i for i in existing_indexes if i.get("name") == idx_name), None)

    idx_payload = {
        "name": idx_name,
        "description": idx_cfg.get("description", ""),
        "embeddings_model_endpoint": idx_cfg["embeddings_model_endpoint"],
        "text_splitting": idx_cfg.get("text_splitting", "SENTENCE"),
        "chunk_size": idx_cfg.get("chunk_size", 100),
        "chunk_overlap": idx_cfg.get("chunk_overlap", 0),
    }

    if existing_index:
        index_id = existing_index["id"]
        needs_update = (
            existing_index.get("description", "") != idx_payload["description"]
            or existing_index.get("embeddings_model_endpoint") != idx_payload["embeddings_model_endpoint"]
            or existing_index.get("text_splitting") != idx_payload["text_splitting"]
            or existing_index.get("chunk_size") != idx_payload["chunk_size"]
            or existing_index.get("chunk_overlap") != idx_payload["chunk_overlap"]
        )
        if needs_update:
            resp = update_resource(client, f"{pc.kb_indexes(kb_id)}/{index_id}", idx_payload)
            log.info("  Updated index '%s' -> id=%s", idx_name, index_id)
            if idx_cfg.get("trigger_indexing", False):
                log.info("  Triggering re-indexing for updated '%s'...", idx_name)
                indexing_resp = client.post(pc.kb_indexings(kb_id, index_id))
                indexing_id = indexing_resp["id"]
                log.info("  Indexing triggered -> id=%s (state=%s)", indexing_id, indexing_resp.get("state"))
                if idx_cfg.get("wait_for_indexing", False):
                    _wait_for_indexing(
                        client, kb_id, index_id, indexing_id,
                        idx_cfg.get("indexing_timeout_seconds", 300),
                    )
        else:
            log.info("  Index '%s' already exists -> id=%s (up to date)", idx_name, index_id)
        return index_id
    resp = client.post(pc.kb_indexes(kb_id), json_body=idx_payload)
    index_id = resp["id"]
    log.info("  Created index '%s' -> id=%s", idx_name, index_id)

    if idx_cfg.get("trigger_indexing", False):
        log.info("  Triggering initial indexing for '%s'...", idx_name)
        indexing_resp = client.post(pc.kb_indexings(kb_id, index_id))
        indexing_id = indexing_resp["id"]
        log.info("  Indexing triggered -> id=%s (state=%s)", indexing_id, indexing_resp.get("state"))
        if idx_cfg.get("wait_for_indexing", False):
            _wait_for_indexing(
                client, kb_id, index_id, indexing_id,
                idx_cfg.get("indexing_timeout_seconds", 300),
            )

    return index_id


def _wait_for_indexing(client: pc.PAISClient, kb_id: str, index_id: str, indexing_id: str, timeout: int) -> None:
    log.info("  Waiting for indexing to complete (timeout=%ds)...", timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = client.get(pc.kb_active_indexing(kb_id, index_id))
            state = resp.get("state", "UNKNOWN")
            log.info("  Indexing state: %s", state)
            if state == "DONE":
                log.info("  Indexing completed successfully.")
                return
            if state == "FAILED":
                raise RuntimeError(f"Indexing {indexing_id} failed: {resp}")
        except RuntimeError as exc:
            if "404" in str(exc):
                log.info("  Indexing appears complete (no active indexing found).")
                return
            raise
        time.sleep(5)
    raise TimeoutError(f"Indexing {indexing_id} did not complete within {timeout}s.")


# ---------------------------------------------------------------------------
# 4. MCP Servers
# ---------------------------------------------------------------------------

def apply_mcp_servers(client: pc.PAISClient, mcp_configs: list[dict], dry_run: bool) -> dict[str, str]:
    """Register or update MCP servers idempotently. Returns {name -> id}."""
    log.info("=== Step 3: MCP Servers ===")
    server_name_to_id: dict[str, str] = {}

    for srv in mcp_configs:
        name = srv["name"]
        payload: dict[str, Any] = {
            "name": name,
            "url": srv["url"],
            "transport": srv.get("transport", "STREAMABLE_HTTP"),
        }
        if srv.get("description"):
            payload["description"] = srv["description"]

        if dry_run:
            log.info("  [dry-run] ensure MCP server '%s' (%s)", name, srv.get("url"))
            server_name_to_id[name] = f"dry-run-srv-{name}"
            continue

        existing = client.find_by_name(pc.MCP_SERVERS, name)
        if existing:
            srv_id = existing["id"]
            server_name_to_id[name] = srv_id
            needs_update = (
                existing.get("url") != payload["url"]
                or existing.get("transport") != payload["transport"]
                or existing.get("description", "") != payload.get("description", "")
            )
            if needs_update:
                resp = update_resource(client, f"{pc.MCP_SERVERS}/{srv_id}", payload)
                log.info("  Updated MCP server '%s' -> id=%s", name, srv_id)
            else:
                log.info("  MCP server '%s' already exists -> id=%s (up to date)", name, srv_id)
            continue

        resp = client.post(pc.MCP_SERVERS, json_body=payload)
        server_name_to_id[name] = resp["id"]
        log.info("  Registered MCP server '%s' -> id=%s (status=%s)", name, resp["id"], resp.get("status"))

    return server_name_to_id


# ---------------------------------------------------------------------------
# 5. Approve MCP Tools
# ---------------------------------------------------------------------------

def extract_tool_keys(t: dict) -> list[str]:
    """Extract all candidate names, titles, origin names, and descriptions from an MCP tool object."""
    keys: list[str] = []
    # Direct top-level string fields
    for f in ("name", "origin_name", "originName", "title", "displayName", "display_name", "description", "label", "tool_configuration_type"):
        v = t.get(f)
        if isinstance(v, str) and v.strip():
            keys.append(v.strip())

    # Stringified or dict 'annotations'
    ann = t.get("annotations")
    if isinstance(ann, str) and ann.strip().startswith("{"):
        try:
            ann = json.loads(ann)
        except Exception:
            ann = None
    if isinstance(ann, dict):
        for f in ("title", "name", "description", "label"):
            v = ann.get(f)
            if isinstance(v, str) and v.strip():
                keys.append(v.strip())

    # Nested 'function', 'tool', 'meta' sub-dicts
    for sub_key in ("function", "tool", "meta"):
        sub = t.get(sub_key)
        if isinstance(sub, dict):
            for f in ("name", "origin_name", "title", "displayName", "display_name", "description"):
                v = sub.get(f)
                if isinstance(v, str) and v.strip():
                    keys.append(v.strip())

    return keys


def approve_mcp_tools(
    client: pc.PAISClient,
    mcp_configs: list[dict],
    agent_configs: list[dict],
    server_name_to_id: dict[str, str],
    dry_run: bool,
    mcp_discovery_timeout: int = 60,
) -> dict[tuple[str, str], str]:
    """Poll and approve the tools named in config (or referenced by agents). Returns {(server, tool) -> tool_id}."""
    log.info("=== Step 4: Approving MCP Tools ===")
    tool_key_to_id: dict[tuple[str, str], str] = {}

    # Collect server -> tool requests from both mcp_servers config AND agents
    requested_tools: dict[str, set[str]] = {}
    for srv in mcp_configs:
        srv_name = srv["name"]
        tools_to_approve = set(srv.get("approve_tools", []))
        if tools_to_approve:
            requested_tools.setdefault(srv_name, set()).update(tools_to_approve)

    for ag in agent_configs:
        for mcp_ref in ag.get("mcp_tools", []):
            srv_name = mcp_ref["server"]
            tool_name = mcp_ref["tool_name"]
            requested_tools.setdefault(srv_name, set()).add(tool_name)

    for srv_name, tools_to_approve in requested_tools.items():
        if dry_run:
            for tool_name in tools_to_approve:
                tool_key_to_id[(srv_name, tool_name)] = f"dry-run-tool-{srv_name}-{tool_name}"
                tool_key_to_id[(srv_name, tool_name.lower())] = f"dry-run-tool-{srv_name}-{tool_name}"
                log.info("  [dry-run] approve tool '%s' on server '%s'", tool_name, srv_name)
            continue

        srv_id = server_name_to_id.get(srv_name)
        if not srv_id:
            # Auto-discover pre-existing MCP server from PAIS API if not created in current run
            existing_srv = client.find_by_name(pc.MCP_SERVERS, srv_name)
            if existing_srv:
                srv_id = existing_srv["id"]
                server_name_to_id[srv_name] = srv_id
                log.info("  Auto-discovered pre-existing MCP server '%s' on PAIS -> id=%s", srv_name, srv_id)
            else:
                log.warning("  Server '%s' not found on PAIS - skipping tool approval", srv_name)
                continue

        deadline = time.time() + mcp_discovery_timeout
        found_tools: dict[str, dict] = {}
        available: list[dict] = []

        while True:
            try:
                available = client.list_all(pc.MCP_TOOLS, params={"server": srv_id, "limit": 999})
            except Exception as exc:
                available = []
                log.debug("Error querying MCP tools for server '%s': %s", srv_name, exc)

            for tool_name in tools_to_approve:
                if tool_name in found_tools:
                    continue

                matching_tool = None
                for t in available:
                    candidate_keys = extract_tool_keys(t)
                    if any(tool_name.lower() == k.lower() or tool_name.lower() in k.lower() for k in candidate_keys):
                        matching_tool = t
                        break

                if not matching_tool and len(available) == 1:
                    matching_tool = available[0]

                if matching_tool:
                    found_tools[tool_name] = matching_tool

            if len(found_tools) == len(tools_to_approve) or time.time() >= deadline:
                break

            missing = tools_to_approve - set(found_tools.keys())
            log.info("  Waiting for MCP tools on server '%s': %s (retry in 5s)...", srv_name, missing)
            time.sleep(5)

        for tool_name in tools_to_approve:
            matching_tool = found_tools.get(tool_name)
            if not matching_tool:
                avail_summary = [extract_tool_keys(t) or t for t in available]
                log.warning(
                    "  Tool '%s' not found on server '%s' after %ds (available tools: %s) - skipping",
                    tool_name, srv_name, mcp_discovery_timeout, avail_summary,
                )
                continue

            tool_id = matching_tool.get("id") or matching_tool.get("tool_id") or matching_tool.get("toolId")
            if matching_tool.get("is_approved"):
                log.info("  Tool '%s' already approved (id=%s)", tool_name, tool_id)
            else:
                client.post(pc.mcp_tool_approval(srv_id, tool_id), json_body={"is_approved": True})
                log.info("  Approved tool '%s' on server '%s' (id=%s)", tool_name, srv_name, tool_id)

            tool_key_to_id[(srv_name, tool_name)] = tool_id
            tool_key_to_id[(srv_name, tool_name.lower())] = tool_id

    return tool_key_to_id


# ---------------------------------------------------------------------------
# 6. Discover REX tools (built-in, auto-created per index)
# ---------------------------------------------------------------------------

def is_valid_uuid(val: Any) -> bool:
    if not val or not isinstance(val, str):
        return False
    try:
        uuid.UUID(val)
        return True
    except ValueError:
        return False


def _extract_all_uuids(obj: Any) -> list[str]:
    """Recursively extract all valid UUID strings from a nested dict/list."""
    uuids: list[str] = []
    if isinstance(obj, dict):
        for v in obj.values():
            if isinstance(v, str) and is_valid_uuid(v):
                uuids.append(v)
            elif isinstance(v, (dict, list)):
                uuids.extend(_extract_all_uuids(v))
    elif isinstance(obj, list):
        for item in obj:
            uuids.extend(_extract_all_uuids(item))
    return uuids


def discover_rex_tools(
    client: pc.PAISClient,
    kb_info: dict[str, dict[str, str]],
    dry_run: bool,
    rex_discovery_timeout: int = 30,
) -> dict[str, str]:
    """Poll built-in/MCP tools across servers until each index's REX search tool appears."""
    log.info("=== Step 5: Discovering REX Search Tools ===")
    kb_name_to_rex_tool_id: dict[str, str] = {}

    expected: dict[str, str] = {}
    for kb_name, info in kb_info.items():
        index_id = info.get("index_id", "")
        if index_id and not index_id.startswith("dry-run"):
            expected[kb_name] = pc.rex_tool_name_for_index(index_id)

    if dry_run or not expected:
        for kb_name in kb_info:
            kb_name_to_rex_tool_id[kb_name] = f"dry-run-rex-{kb_name}"
        if dry_run:
            log.info("  [dry-run] REX tools resolved to placeholders")
        return kb_name_to_rex_tool_id

    deadline = time.time() + rex_discovery_timeout
    found: set[str] = set()

    while time.time() < deadline:
        all_tools: list[tuple[str, dict]] = []

        # 1. ALWAYS query global tools endpoint FIRST (built-in/system tools)
        try:
            for t in client.list_all(pc.MCP_TOOLS, params={"limit": 999}):
                all_tools.append(("", t))
        except Exception as exc:
            log.debug("Global MCP tools list query: %s", exc)

        # 2. Query tools per registered MCP server
        try:
            mcp_servers = client.list_all(pc.MCP_SERVERS)
            for srv in mcp_servers:
                srv_id = srv.get("id")
                if srv_id:
                    srv_tools = client.list_all(pc.MCP_TOOLS, params={"server": srv_id, "limit": 999})
                    for t in srv_tools:
                        all_tools.append((srv_id, t))
        except Exception as exc:
            log.debug("Server-specific MCP tools list query: %s", exc)

        for kb_name, expected_rex_name in expected.items():
            if kb_name in found:
                continue

            info = kb_info.get(kb_name, {})
            idx_id = info.get("index_id", "")
            kb_id = info.get("kb_id", "")
            idx_hex = idx_id.replace("-", "") if idx_id else ""

            matching_srv_id = None
            matching_tool = None

            for srv_id, t in all_tools:
                candidate_keys = extract_tool_keys(t)
                t_id = str(t.get("id") or t.get("tool_id") or t.get("toolId") or "")

                if any(
                    expected_rex_name.lower() in k.lower()
                    or (idx_hex and idx_hex.lower() in k.lower())
                    or kb_name.lower() == k.lower()
                    or (idx_id and idx_id in k)
                    or (kb_id and kb_id in k)
                    for k in candidate_keys
                ) or (
                    idx_id and idx_id in t_id
                ) or (
                    kb_id and kb_id in t_id
                ) or (
                    idx_hex and idx_hex in t_id
                ):
                    matching_srv_id = srv_id
                    matching_tool = t
                    break

            if matching_tool:
                tool_uuid = matching_tool.get("id") or matching_tool.get("tool_id") or matching_tool.get("toolId")
                if is_valid_uuid(tool_uuid) and tool_uuid != kb_id and tool_uuid != idx_id:
                    if matching_srv_id and not matching_tool.get("is_approved"):
                        try:
                            client.post(pc.mcp_tool_approval(matching_srv_id, tool_uuid), json_body={"is_approved": True})
                            log.info("  Auto-approved REX search tool '%s' on server '%s'", tool_uuid, matching_srv_id)
                        except Exception:
                            pass
                    kb_name_to_rex_tool_id[kb_name] = tool_uuid
                    found.add(kb_name)
                    log.info("  Found REX search tool for KB '%s' (tool_id=%s)", kb_name, tool_uuid)

        # 3. Direct lookup on KB/Index details if not matched in tools
        for kb_name in set(expected) - found:
            info = kb_info.get(kb_name, {})
            kb_id = info.get("kb_id", "")
            idx_id = info.get("index_id", "")

            found_uuid = None
            if kb_id:
                try:
                    kb_detail = client.get(f"{pc.KNOWLEDGE_BASES}/{kb_id}")
                    for val in _extract_all_uuids(kb_detail):
                        if val != kb_id and val != idx_id:
                            found_uuid = val
                            break
                except Exception:
                    pass

            if not found_uuid and kb_id and idx_id:
                try:
                    idx_detail = client.get(f"{pc.KNOWLEDGE_BASES}/{kb_id}/indexes/{idx_id}")
                    for val in _extract_all_uuids(idx_detail):
                        if val != kb_id and val != idx_id:
                            found_uuid = val
                            break
                except Exception:
                    pass

            if found_uuid:
                kb_name_to_rex_tool_id[kb_name] = found_uuid
                found.add(kb_name)
                log.info("  Resolved KB '%s' search tool ID from KB/Index details -> %s", kb_name, found_uuid)

        if len(found) == len(expected):
            break
        log.info("  Waiting for REX tools: %s (retry in 5s)...", set(expected) - found)
        time.sleep(5)

    missing = set(expected) - found
    if missing:
        log.warning("  REX search tool UUID not found for KBs: %s (will skip linking bad IDs)", missing)

    return kb_name_to_rex_tool_id


# ---------------------------------------------------------------------------
# 7. Create/Update Agents
# ---------------------------------------------------------------------------

def apply_agents(
    client: pc.PAISClient,
    agent_configs: list[dict],
    kb_name_to_rex_tool_id: dict[str, str],
    mcp_tool_key_to_id: dict[tuple[str, str], str],
    dry_run: bool,
) -> list[dict]:
    """Create agents (or update existing ones in place by name)."""
    log.info("=== Step 6: Agents ===")
    result_agents: list[dict] = []

    for ag in agent_configs:
        ag_name = ag["name"]
        existing = client.find_by_name(pc.AGENTS, ag_name) if not dry_run else None
        tools = _build_agent_tools(client, ag, ag_name, kb_name_to_rex_tool_id, mcp_tool_key_to_id, existing, dry_run)

        payload: dict[str, Any] = {
            "name": ag_name,
            "description": ag.get("description", ""),
            "model": ag["model"],
            "instructions": ag.get("instructions", "You are a helpful assistant."),
            "tools": tools,
            "completion_role": ag.get("completion_role", "assistant"),
            "session_max_length": ag.get("session_max_length", 10000),
            "session_summarization_strategy": ag.get("session_summarization_strategy", "delete_oldest"),
            "chat_system_instruction_mode": ag.get("chat_system_instruction_mode", "system-message"),
        }
        if ag.get("index_reference_format") is not None:
            payload["index_reference_format"] = ag["index_reference_format"]
        if ag.get("index_reference_delimiter") is not None:
            payload["index_reference_delimiter"] = ag["index_reference_delimiter"]
        if ag.get("session_max_ttl") is not None:
            payload["session_max_ttl"] = ag["session_max_ttl"]
        if ag.get("metadata"):
            payload["metadata"] = ag["metadata"]

        if dry_run:
            log.info("  [dry-run] ensure agent '%s' with %d tool(s):", ag_name, len(tools))
            log.info("  %s", json.dumps(payload, indent=4))
            result_agents.append({"name": ag_name, "id": f"dry-run-agent-{ag_name}", "status": "DRY_RUN"})
            continue

        if existing:
            agent_id = existing["id"]
            resp = update_resource(client, f"{pc.AGENTS}/{agent_id}", payload)
            log.info("  Updated agent '%s' -> id=%s (status=%s)", ag_name, agent_id, resp.get("status"))
        else:
            resp = client.post(pc.AGENTS, json_body=payload)
            log.info("  Created agent '%s' -> id=%s (status=%s)", ag_name, resp.get("id"), resp.get("status"))
        result_agents.append(resp)

    return result_agents


def _build_agent_tools(
    client: pc.PAISClient,
    ag: dict,
    ag_name: str,
    kb_name_to_rex_tool_id: dict[str, str],
    mcp_tool_key_to_id: dict[tuple[str, str], str],
    existing_agent: dict | None,
    dry_run: bool,
) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []

    for kb_ref in ag.get("knowledge_bases", []):
        if isinstance(kb_ref, str):
            kb_ref = {"name": kb_ref}
        kb_name = kb_ref["name"]

        # 1. Direct explicit tool_id in config.yaml
        rex_tool_id = kb_ref.get("tool_id")

        # 2. Look up in kb_name_to_rex_tool_id map from Step 5
        if not rex_tool_id:
            rex_tool_id = kb_name_to_rex_tool_id.get(kb_name)

        # 3. Check existing agent on PAIS for attached KB search tool
        if not rex_tool_id and existing_agent:
            for t in existing_agent.get("tools", []):
                if t.get("link_type") == "PAIS_KNOWLEDGE_BASE_INDEX_SEARCH_TOOL_LINK" and is_valid_uuid(t.get("tool_id")):
                    rex_tool_id = t["tool_id"]
                    log.info("  Agent '%s': Reused KB search tool UUID from existing agent -> %s", ag_name, rex_tool_id)
                    break

        # 4. Check all agents on PAIS for any attached KB search tool
        if not rex_tool_id and not dry_run:
            try:
                all_agents = client.list_all(pc.AGENTS)
                for other_ag in all_agents:
                    for t in other_ag.get("tools", []):
                        if t.get("link_type") == "PAIS_KNOWLEDGE_BASE_INDEX_SEARCH_TOOL_LINK" and is_valid_uuid(t.get("tool_id")):
                            rex_tool_id = t["tool_id"]
                            log.info("  Agent '%s': Reused KB search tool UUID from PAIS agent '%s' -> %s", ag_name, other_ag.get("name"), rex_tool_id)
                            break
                    if rex_tool_id:
                        break
            except Exception:
                pass

        # 5. Check pre-existing Knowledge Base on PAIS
        if not rex_tool_id and not dry_run:
            existing_kb = client.find_by_name(pc.KNOWLEDGE_BASES, kb_name)
            if existing_kb:
                kb_id = existing_kb.get("id")
                idx_id = existing_kb.get("index", {}).get("id")
                for val in _extract_all_uuids(existing_kb):
                    if val != kb_id and val != idx_id:
                        rex_tool_id = val
                        break
                if rex_tool_id:
                    log.info("  Agent '%s': Resolved search tool UUID for pre-existing KB '%s' -> %s", ag_name, kb_name, rex_tool_id)

        if not rex_tool_id or not is_valid_uuid(rex_tool_id):
            log.warning("  Agent '%s': REX search tool UUID for KB '%s' unavailable - skipping KB search tool link", ag_name, kb_name)
            continue

        entry: dict[str, Any] = {
            "link_type": "PAIS_KNOWLEDGE_BASE_INDEX_SEARCH_TOOL_LINK",
            "tool_id": rex_tool_id,
        }
        if "top_n" in kb_ref:
            entry["top_n"] = kb_ref["top_n"]
        if "similarity_cutoff" in kb_ref:
            entry["similarity_cutoff"] = kb_ref["similarity_cutoff"]
        tools.append(entry)
        log.info("  Agent '%s': + KB search tool for '%s'", ag_name, kb_name)

    for mcp_ref in ag.get("mcp_tools", []):
        srv_name = mcp_ref["server"]
        tool_name = mcp_ref["tool_name"]
        tool_id = mcp_tool_key_to_id.get((srv_name, tool_name)) or mcp_tool_key_to_id.get((srv_name, tool_name.lower()))

        if not tool_id and not dry_run:
            # On-the-fly auto-discovery of pre-existing MCP server & tool on PAIS
            existing_srv = client.find_by_name(pc.MCP_SERVERS, srv_name)
            if existing_srv:
                srv_id = existing_srv["id"]
                available = client.list_all(pc.MCP_TOOLS, params={"server": srv_id})
                matching_tool = None
                for t in available:
                    candidate_keys = extract_tool_keys(t)
                    if any(tool_name.lower() == k.lower() or tool_name.lower() in k.lower() for k in candidate_keys):
                        matching_tool = t
                        break

                if not matching_tool and len(available) == 1:
                    matching_tool = available[0]

                if matching_tool:
                    tool_id = matching_tool.get("id") or matching_tool.get("tool_id") or matching_tool.get("toolId")
                    if not matching_tool.get("is_approved"):
                        client.post(pc.mcp_tool_approval(srv_id, tool_id), json_body={"is_approved": True})
                        log.info("  Approved pre-existing tool '%s' on server '%s' (id=%s)", tool_name, srv_name, tool_id)
                    log.info("  Agent '%s': Auto-discovered MCP tool '%s' on '%s' -> id=%s", ag_name, tool_name, srv_name, tool_id)

        if not tool_id:
            log.warning(
                "  Agent '%s': MCP tool '%s' on '%s' not approved/found - skipping",
                ag_name, tool_name, srv_name,
            )
            continue
        tools.append({"link_type": "GENERIC_MCP_TOOL_LINK", "tool_id": tool_id})
        log.info("  Agent '%s': + MCP tool '%s' from '%s' (id=%s)", ag_name, tool_name, srv_name, tool_id)

    return tools


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Apply PAIS resources from a YAML config (GitOps apply).")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML config file")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making API calls")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args(argv)

    pc.setup_logging(args.verbose)
    if args.dry_run:
        log.info("*** DRY-RUN MODE - no API calls will be made ***")

    log.info("Loading config from '%s'...", args.config)
    cfg = pc.load_config(args.config)

    # Step 0: Kubernetes Model Endpoint & Inference Gateway Route reconciliation
    k8s_manifests = apply_kubernetes_resources(cfg, args.dry_run, config_path=args.config)

    pais_cfg = cfg.get("pais", {})
    base_url, auth_cfg, verify_ssl = pc.resolve_connection(pais_cfg)
    log.info("Target PAIS REST instance connected.")
    if auth_cfg.get("token_url"):
        log.info("Target IdP/Authentik configured.")

    if args.dry_run:
        client = pc.PAISClient.offline(base_url)
    else:
        client = pc.PAISClient(base_url, pc.build_auth(auth_cfg, verify_ssl=verify_ssl), verify_ssl=verify_ssl)

    try:
        ds_name_to_id = apply_data_sources(client, cfg.get("data_sources", []), args.dry_run)
        kb_info = apply_knowledge_bases(client, cfg.get("knowledge_bases", []), ds_name_to_id, args.dry_run)
        server_name_to_id = apply_mcp_servers(client, cfg.get("mcp_servers", []), args.dry_run)
        mcp_timeout = pais_cfg.get("mcp_tool_discovery_timeout_seconds", 60)
        mcp_tool_key_to_id = approve_mcp_tools(client, cfg.get("mcp_servers", []), cfg.get("agents", []), server_name_to_id, args.dry_run, mcp_discovery_timeout=mcp_timeout)
        rex_timeout = pais_cfg.get("rex_discovery_timeout_seconds", 30)
        kb_rex = discover_rex_tools(client, kb_info, args.dry_run, rex_timeout)
        agents = apply_agents(client, cfg.get("agents", []), kb_rex, mcp_tool_key_to_id, args.dry_run)

        if setup_openwebui.should_remove_openwebui(cfg):
            owui_status = "Removed" if setup_openwebui.remove_openwebui_integration(cfg, dry_run=args.dry_run) else "Removal Failed/Skipped"
        else:
            owui_status = "Configured" if setup_openwebui.apply_openwebui_integration(cfg, client=client, dry_run=args.dry_run) else "Skipped/Disabled"

        log.info("")
        log.info("=== Apply Complete ===")
        log.info("K8s Manifests  : %d generated/applied", len(k8s_manifests))
        log.info("Data Sources   : %d", len(ds_name_to_id))
        log.info("Knowledge Bases: %d", len(kb_info))
        log.info("MCP Servers    : %d", len(server_name_to_id))
        log.info("MCP Tools      : %d approved", len(mcp_tool_key_to_id))
        log.info("Agents         : %d", len(agents))
        for agent in agents:
            log.info("  -> '%s'  id=%s  status=%s", agent.get("name"), agent.get("id"), agent.get("status"))
        log.info("OpenWebUI Setup: %s", owui_status)

    except (RuntimeError, ValueError, TimeoutError) as exc:
        log.error("FATAL: %s", exc)
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
