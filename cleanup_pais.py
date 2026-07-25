"""
VCF Private AI Services (PAIS) - GitOps Cleanup Script
======================================================
Compares the *previous* version of the config file against the *current*
version and removes from the PAIS instance whatever was deleted from the
config. This is the deletion half of the GitOps loop (``setup_pais.py``
handles additions/updates).

What it detects and removes (by name) when an item disappears from config:

  * ModelEndpoints & InferenceGatewayRoutes -> kubectl delete CRDs
  * Agents removed                 -> DELETE agent
  * Knowledge bases removed        -> DELETE knowledge base (cascades index + REX tool)
  * MCP servers removed            -> DELETE MCP server (cascades its tools)
  * Data sources removed           -> DELETE data source
  * Tools dropped from approve_tools (server kept) -> un-approve tool
  * Data sources unlinked from a (kept) KB         -> DELETE the KB<->DS link

Deletion order respects dependencies (agents first, then KBs/servers, then
data sources and model CRDs) so the API does not reject a delete whose object is
still in use (e.g. an MCP server whose tool is still linked to an agent -> HTTP 409).

Usage
-----
  python cleanup_pais.py --old-config old.yaml --new-config config.yaml \
      [--dry-run] [--verbose]

If --old-config is omitted or missing, nothing is removed (treated as the
first apply).
"""

from __future__ import annotations

import argparse
import os
import sys

import k8s_manager as km
import pais_client as pc
from pais_client import log


# ---------------------------------------------------------------------------
# Diff helpers
# ---------------------------------------------------------------------------

def _names(items: list[dict] | None) -> set[str]:
    return {item["name"] for item in (items or []) if isinstance(item, dict) and item.get("name")}


def _by_name(items: list[dict] | None) -> dict[str, dict]:
    return {item["name"]: item for item in (items or []) if isinstance(item, dict) and item.get("name")}


# ---------------------------------------------------------------------------
# Removal actions
# ---------------------------------------------------------------------------

def delete_removed_agents(client: pc.PAISClient, old: dict, new: dict, dry_run: bool) -> int:
    removed = _names(old.get("agents")) - _names(new.get("agents"))
    if not removed:
        return 0
    log.info("=== Removing Agents: %s ===", removed)
    count = 0
    for name in sorted(removed):
        if dry_run:
            log.info("  [dry-run] delete agent '%s'", name)
            count += 1
            continue
        obj = client.find_by_name(pc.AGENTS, name)
        if not obj:
            log.info("  Agent '%s' not found on server (already gone) - skip", name)
            continue
        client.delete(f"{pc.AGENTS}/{obj['id']}")
        log.info("  Deleted agent '%s' (id=%s)", name, obj["id"])
        count += 1
    return count


def revoke_removed_tool_approvals(client: pc.PAISClient, old: dict, new: dict, dry_run: bool) -> int:
    """For servers present in both configs, un-approve tools dropped from approve_tools."""
    old_servers = _by_name(old.get("mcp_servers"))
    new_servers = _by_name(new.get("mcp_servers"))
    surviving = set(old_servers) & set(new_servers)

    count = 0
    for srv_name in sorted(surviving):
        old_tools = set(old_servers[srv_name].get("approve_tools", []) or [])
        new_tools = set(new_servers[srv_name].get("approve_tools", []) or [])
        revoked = old_tools - new_tools
        if not revoked:
            continue
        log.info("=== Un-approving tools on server '%s': %s ===", srv_name, revoked)

        if dry_run:
            for tool_name in sorted(revoked):
                log.info("  [dry-run] un-approve tool '%s' on '%s'", tool_name, srv_name)
                count += 1
            continue

        srv_obj = client.find_by_name(pc.MCP_SERVERS, srv_name)
        if not srv_obj:
            log.info("  Server '%s' not found - skip tool revocation", srv_name)
            continue
        srv_id = srv_obj["id"]
        tool_by_name = {t["name"]: t for t in client.list_all(pc.MCP_TOOLS, params={"server": srv_id})}
        for tool_name in sorted(revoked):
            tool = tool_by_name.get(tool_name)
            if not tool:
                log.info("  Tool '%s' not found on '%s' - skip", tool_name, srv_name)
                continue
            client.post(pc.mcp_tool_approval(srv_id, tool["id"]), json_body={"is_approved": False})
            log.info("  Un-approved tool '%s' on '%s'", tool_name, srv_name)
            count += 1
    return count


def unlink_removed_data_sources(client: pc.PAISClient, old: dict, new: dict, dry_run: bool) -> int:
    """For KBs present in both configs, unlink data sources dropped from the KB."""
    old_kbs = _by_name(old.get("knowledge_bases"))
    new_kbs = _by_name(new.get("knowledge_bases"))
    surviving = set(old_kbs) & set(new_kbs)

    count = 0
    for kb_name in sorted(surviving):
        old_ds = set(old_kbs[kb_name].get("data_sources", []) or [])
        new_ds = set(new_kbs[kb_name].get("data_sources", []) or [])
        unlinked = old_ds - new_ds
        if not unlinked:
            continue
        log.info("=== Unlinking data sources from KB '%s': %s ===", kb_name, unlinked)

        if dry_run:
            for ds_name in sorted(unlinked):
                log.info("  [dry-run] unlink data source '%s' from KB '%s'", ds_name, kb_name)
                count += 1
            continue

        kb_obj = client.find_by_name(pc.KNOWLEDGE_BASES, kb_name)
        if not kb_obj:
            log.info("  KB '%s' not found - skip unlinking", kb_name)
            continue
        kb_id = kb_obj["id"]
        links = client.list_all(pc.kb_data_source_links(kb_id))
        link_by_ds_name = {
            (lnk.get("data_source") or {}).get("name"): lnk.get("id")
            for lnk in links
        }
        for ds_name in sorted(unlinked):
            link_id = link_by_ds_name.get(ds_name)
            if not link_id:
                log.info("  Data source '%s' not linked to KB '%s' - skip", ds_name, kb_name)
                continue
            client.delete(f"{pc.kb_data_source_links(kb_id)}/{link_id}")
            log.info("  Unlinked data source '%s' from KB '%s'", ds_name, kb_name)
            count += 1
    return count


def delete_removed_knowledge_bases(client: pc.PAISClient, old: dict, new: dict, dry_run: bool) -> int:
    removed = _names(old.get("knowledge_bases")) - _names(new.get("knowledge_bases"))
    if not removed:
        return 0
    log.info("=== Removing Knowledge Bases: %s ===", removed)
    count = 0
    for name in sorted(removed):
        if dry_run:
            log.info("  [dry-run] delete knowledge base '%s'", name)
            count += 1
            continue
        obj = client.find_by_name(pc.KNOWLEDGE_BASES, name)
        if not obj:
            log.info("  KB '%s' not found (already gone) - skip", name)
            continue
        client.delete(f"{pc.KNOWLEDGE_BASES}/{obj['id']}")
        log.info("  Deleted knowledge base '%s' (id=%s)", name, obj["id"])
        count += 1
    return count


def delete_removed_mcp_servers(client: pc.PAISClient, old: dict, new: dict, dry_run: bool) -> int:
    removed = _names(old.get("mcp_servers")) - _names(new.get("mcp_servers"))
    if not removed:
        return 0
    log.info("=== Removing MCP Servers: %s ===", removed)
    count = 0
    for name in sorted(removed):
        if dry_run:
            log.info("  [dry-run] delete MCP server '%s'", name)
            count += 1
            continue
        obj = client.find_by_name(pc.MCP_SERVERS, name)
        if not obj:
            log.info("  MCP server '%s' not found (already gone) - skip", name)
            continue
        client.delete(f"{pc.MCP_SERVERS}/{obj['id']}")
        log.info("  Deleted MCP server '%s' (id=%s)", name, obj["id"])
        count += 1
    return count


def delete_removed_data_sources(client: pc.PAISClient, old: dict, new: dict, dry_run: bool) -> int:
    removed = _names(old.get("data_sources")) - _names(new.get("data_sources"))
    if not removed:
        return 0
    log.info("=== Removing Data Sources: %s ===", removed)
    count = 0
    for name in sorted(removed):
        if dry_run:
            log.info("  [dry-run] delete data source '%s'", name)
            count += 1
            continue
        obj = client.find_by_name(pc.DATA_SOURCES, name)
        if not obj:
            log.info("  Data source '%s' not found (already gone) - skip", name)
            continue
        client.delete(f"{pc.DATA_SOURCES}/{obj['id']}")
        log.info("  Deleted data source '%s' (id=%s)", name, obj["id"])
        count += 1
    return count


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Remove PAIS resources deleted from config (GitOps cleanup).")
    parser.add_argument("--old-config", help="Previous version of the config (e.g. from the prior git commit)")
    parser.add_argument("--new-config", default="config.yaml", help="Current config file")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making API calls")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    args = parser.parse_args(argv)

    pc.setup_logging(args.verbose)
    if args.dry_run:
        log.info("*** DRY-RUN MODE - no API calls will be made ***")

    if not args.old_config or not os.path.exists(args.old_config):
        log.info("No previous config provided/found - nothing to remove (first apply).")
        return

    old_cfg = pc.load_config(args.old_config)
    new_cfg = pc.load_config(args.new_config)

    # 1. Clean up removed K8s ModelEndpoints & InferenceGatewayRoutes
    km.delete_removed_k8s_resources(old_cfg, new_cfg, dry_run=args.dry_run)

    # 2. Clean up REST API resources
    base_url, auth_cfg, verify_ssl = pc.resolve_connection(new_cfg.get("pais", {}))
    log.info("Target PAIS REST instance connected.")

    if args.dry_run:
        client = pc.PAISClient.offline(base_url)
    else:
        client = pc.PAISClient(base_url, pc.build_auth(auth_cfg, verify_ssl=verify_ssl), verify_ssl=verify_ssl)

    try:
        agents_removed = delete_removed_agents(client, old_cfg, new_cfg, args.dry_run)
        tools_revoked = revoke_removed_tool_approvals(client, old_cfg, new_cfg, args.dry_run)
        links_removed = unlink_removed_data_sources(client, old_cfg, new_cfg, args.dry_run)
        kbs_removed = delete_removed_knowledge_bases(client, old_cfg, new_cfg, args.dry_run)
        servers_removed = delete_removed_mcp_servers(client, old_cfg, new_cfg, args.dry_run)
        ds_removed = delete_removed_data_sources(client, old_cfg, new_cfg, args.dry_run)

        total = agents_removed + tools_revoked + links_removed + kbs_removed + servers_removed + ds_removed
        log.info("")
        log.info("=== Cleanup Complete ===")
        log.info("Agents deleted        : %d", agents_removed)
        log.info("Tool approvals revoked: %d", tools_revoked)
        log.info("DS links removed      : %d", links_removed)
        log.info("Knowledge bases deleted: %d", kbs_removed)
        log.info("MCP servers deleted   : %d", servers_removed)
        log.info("Data sources deleted  : %d", ds_removed)
        if total == 0:
            log.info("No removals detected between the two config versions.")

    except (RuntimeError, ValueError) as exc:
        log.error("FATAL: %s", exc)
        sys.exit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
