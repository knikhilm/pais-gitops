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
from pais_client import log


# ---------------------------------------------------------------------------
# 0. Kubernetes Model Endpoints & Inference Gateway Routes
# ---------------------------------------------------------------------------

def apply_kubernetes_resources(config: dict, dry_run: bool, manifests_out_dir: str = "k8s-manifests") -> list[dict]:
    """
    Generate and apply Kubernetes Custom Resources for ModelEndpoints and InferenceGatewayRoutes.
    """
    log.info("=== Step 0: Kubernetes ModelEndpoints & InferenceGatewayRoutes ===")
    manifests = km.generate_k8s_manifests(config)

    if manifests:
        out_file = os.path.join(manifests_out_dir, "pais-resources.yaml")
        km.write_manifests_file(manifests, out_file)
        km.apply_k8s_manifests(config, manifests, dry_run=dry_run)
        km.wait_for_model_endpoints(config, dry_run=dry_run)
    else:
        log.info("  No Kubernetes ModelEndpoints or InferenceGatewayRoutes configured in config.")

    return manifests


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

        if dry_run:
            log.info("  [dry-run] ensure data source '%s' (type=%s)", name, ds_type)
            name_to_id[name] = f"dry-run-ds-{name}"
            continue

        existing = client.find_by_name(pc.DATA_SOURCES, name)
        if existing:
            name_to_id[name] = existing["id"]
            log.info("  Data source '%s' already exists -> id=%s (skip)", name, existing["id"])
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

        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "type": ds_type,
            "origin_url": origin_url,
            "credentials": credentials,
        }
        if ds.get("options"):
            payload["options"] = ds["options"]

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
            log.info("  Knowledge base '%s' already exists -> id=%s (skip create)", kb_name, kb_id)
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

    if existing_index:
        index_id = existing_index["id"]
        log.info("  Index '%s' already exists -> id=%s (skip create)", idx_name, index_id)
        return index_id

    idx_payload = {
        "name": idx_name,
        "description": idx_cfg.get("description", ""),
        "embeddings_model_endpoint": idx_cfg["embeddings_model_endpoint"],
        "text_splitting": idx_cfg.get("text_splitting", "SENTENCE"),
        "chunk_size": idx_cfg.get("chunk_size", 100),
        "chunk_overlap": idx_cfg.get("chunk_overlap", 0),
    }
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
    """Register MCP servers that don't yet exist. Returns {name -> id}."""
    log.info("=== Step 3: MCP Servers ===")
    server_name_to_id: dict[str, str] = {}

    for srv in mcp_configs:
        name = srv["name"]

        if dry_run:
            log.info("  [dry-run] ensure MCP server '%s' (%s)", name, srv.get("url"))
            server_name_to_id[name] = f"dry-run-srv-{name}"
            continue

        existing = client.find_by_name(pc.MCP_SERVERS, name)
        if existing:
            server_name_to_id[name] = existing["id"]
            log.info("  MCP server '%s' already exists -> id=%s (skip)", name, existing["id"])
            continue

        payload: dict[str, Any] = {
            "name": name,
            "url": srv["url"],
            "transport": srv.get("transport", "STREAMABLE_HTTP"),
        }
        if srv.get("description"):
            payload["description"] = srv["description"]

        resp = client.post(pc.MCP_SERVERS, json_body=payload)
        server_name_to_id[name] = resp["id"]
        log.info("  Registered MCP server '%s' -> id=%s (status=%s)", name, resp["id"], resp.get("status"))

    return server_name_to_id


# ---------------------------------------------------------------------------
# 5. Approve MCP Tools
# ---------------------------------------------------------------------------

def extract_tool_keys(t: dict) -> list[str]:
    """Extract all candidate names, titles, and descriptions from an MCP tool object."""
    keys: list[str] = []
    # Direct top-level string fields
    for f in ("name", "title", "displayName", "display_name", "description", "label"):
        v = t.get(f)
        if isinstance(v, str) and v.strip():
            keys.append(v.strip())

    # Nested 'function' sub-dict (e.g. OpenAI / MCP tool schema)
    fn = t.get("function")
    if isinstance(fn, dict):
        for f in ("name", "title", "displayName", "display_name", "description"):
            v = fn.get(f)
            if isinstance(v, str) and v.strip():
                keys.append(v.strip())

    # Nested 'tool' sub-dict
    tool_sub = t.get("tool")
    if isinstance(tool_sub, dict):
        for f in ("name", "title", "displayName", "display_name", "description"):
            v = tool_sub.get(f)
            if isinstance(v, str) and v.strip():
                keys.append(v.strip())

    # Nested 'meta' sub-dict
    meta = t.get("meta")
    if isinstance(meta, dict):
        for f in ("name", "title", "displayName", "display_name", "description"):
            v = meta.get(f)
            if isinstance(v, str) and v.strip():
                keys.append(v.strip())

    return keys


def approve_mcp_tools(
    client: pc.PAISClient,
    mcp_configs: list[dict],
    agent_configs: list[dict],
    server_name_to_id: dict[str, str],
    dry_run: bool,
) -> dict[tuple[str, str], str]:
    """Approve the tools named in config (or referenced by agents). Returns {(server, tool) -> tool_id}."""
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

        available = client.list_all(pc.MCP_TOOLS, params={"server": srv_id})

        for tool_name in tools_to_approve:
            matching_tool = None
            for t in available:
                candidate_keys = extract_tool_keys(t)
                if any(tool_name.lower() == k.lower() or tool_name.lower() in k.lower() for k in candidate_keys):
                    matching_tool = t
                    break

            if not matching_tool and len(available) == 1:
                matching_tool = available[0]
                log.info("  Matched tool '%s' on server '%s' as the single available tool on server", tool_name, srv_name)

            if not matching_tool:
                avail_summary = [extract_tool_keys(t) or t for t in available]
                log.warning(
                    "  Tool '%s' not found on server '%s' (available tools: %s) - skipping",
                    tool_name, srv_name, avail_summary,
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
            for t in client.list_all(pc.MCP_TOOLS):
                all_tools.append(("", t))
        except Exception as exc:
            log.debug("Global MCP tools list query: %s", exc)

        # 2. Query tools per registered MCP server
        try:
            mcp_servers = client.list_all(pc.MCP_SERVERS)
            for srv in mcp_servers:
                srv_id = srv.get("id")
                if srv_id:
                    srv_tools = client.list_all(pc.MCP_TOOLS, params={"server": srv_id})
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
        tools = _build_agent_tools(client, ag, ag_name, kb_name_to_rex_tool_id, mcp_tool_key_to_id, dry_run)

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
        if ag.get("session_max_ttl") is not None:
            payload["session_max_ttl"] = ag["session_max_ttl"]
        if ag.get("metadata"):
            payload["metadata"] = ag["metadata"]

        if dry_run:
            log.info("  [dry-run] ensure agent '%s' with %d tool(s):", ag_name, len(tools))
            log.info("  %s", json.dumps(payload, indent=4))
            result_agents.append({"name": ag_name, "id": f"dry-run-agent-{ag_name}", "status": "DRY_RUN"})
            continue

        existing = client.find_by_name(pc.AGENTS, ag_name)
        if existing:
            agent_id = existing["id"]
            resp = client.post(f"{pc.AGENTS}/{agent_id}", json_body=payload)
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
    dry_run: bool,
) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []

    for kb_ref in ag.get("knowledge_bases", []):
        if isinstance(kb_ref, str):
            kb_ref = {"name": kb_ref}
        kb_name = kb_ref["name"]
        rex_tool_id = kb_name_to_rex_tool_id.get(kb_name)
        if not rex_tool_id and not dry_run:
            # Auto-discover pre-existing Knowledge Base on PAIS
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
    k8s_manifests = apply_kubernetes_resources(cfg, args.dry_run)

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
        mcp_tool_key_to_id = approve_mcp_tools(client, cfg.get("mcp_servers", []), cfg.get("agents", []), server_name_to_id, args.dry_run)
        rex_timeout = pais_cfg.get("rex_discovery_timeout_seconds", 30)
        kb_rex = discover_rex_tools(client, kb_info, args.dry_run, rex_timeout)
        agents = apply_agents(client, cfg.get("agents", []), kb_rex, mcp_tool_key_to_id, args.dry_run)

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

    except (RuntimeError, ValueError, TimeoutError) as exc:
        log.error("FATAL: %s", exc)
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
