"""
VCF Private AI Services (PAIS) - Multi-Tenant Reconciliation Orchestrator
========================================================================
Discovers and reconciles all tenant configurations (under `tenants/*/config.yaml`
and root `config.yaml` if present) sequentially in **Priority Order**.

Priority/Precedence Rules:
  - Each `config.yaml` can specify `pais.priority: <number>` (e.g. 1 for infrastructure/shared org, 10 for applications).
  - Lower priority numbers run EARLIER (e.g., Priority 1 runs before Priority 10).
  - Unspecified priorities default to 100.

Usage:
  python reconcile_all.py [--dry-run] [--verbose] [--old-config-dir <dir>]
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import subprocess
import sys
import tempfile

import yaml

import cleanup_pais
import setup_pais
from pais_client import log, setup_logging


def discover_config_files() -> list[tuple[int, str]]:
    """
    Discover all active tenant config.yaml files, returning a list of
    (priority, path) tuples sorted by priority (lowest number first).
    """
    raw_configs: list[str] = []

    # 1. Root config.yaml if present
    if os.path.exists("config.yaml"):
        raw_configs.append("config.yaml")

    # 2. Tenants config files (tenants/*/config.yaml)
    tenant_matches = sorted(glob.glob("tenants/*/config.yaml"))
    for match in tenant_matches:
        norm_match = match.replace("\\", "/")
        if norm_match not in raw_configs:
            raw_configs.append(norm_match)

    configs_with_priority: list[tuple[int, str]] = []
    for path in raw_configs:
        priority = 100
        try:
            with open(path, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
                priority = int(data.get("pais", {}).get("priority", 100))
        except Exception as exc:
            log.warning("Could not read priority from '%s': %s (defaulting to 100)", path, exc)
        configs_with_priority.append((priority, path))

    # Sort by priority ascending, then by path alphabetically
    configs_with_priority.sort(key=lambda item: (item[0], item[1]))
    return configs_with_priority


def extract_previous_config(cfg_path: str, old_config_dir: str | None = None) -> str | None:
    """
    Attempt to extract the previous version of a tenant config from git or old_config_dir.
    Returns path to temporary file containing old config, or None if not found.
    """
    # 1. Check old_config_dir if provided
    if old_config_dir:
        norm = cfg_path.replace("\\", "/").strip("/")
        tenant_name = norm.split("/")[-2] if "/" in norm else "root"
        candidate = os.path.join(old_config_dir, f"{tenant_name}_old.yaml")
        if os.path.exists(candidate):
            return candidate

    # 2. Attempt git show from BEFORE commit or HEAD~1
    before_commit = os.environ.get("GITHUB_EVENT_BEFORE") or os.environ.get("BEFORE_COMMIT")
    git_refs = []
    if before_commit and before_commit != "0000000000000000000000000000000000000000":
        git_refs.append(before_commit)
    git_refs.append("HEAD~1")

    norm_cfg_path = cfg_path.replace("\\", "/")

    for ref in git_refs:
        try:
            res = subprocess.run(
                ["git", "show", f"{ref}:{norm_cfg_path}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if res.returncode == 0 and res.stdout.strip():
                tmp_fh = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8")
                tmp_fh.write(res.stdout)
                tmp_fh.close()
                log.info("  Extracted previous version of '%s' from git ref '%s'", norm_cfg_path, ref)
                return tmp_fh.name
        except Exception:
            pass

    return None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile all tenant PAIS configurations in Priority Order (Multi-Tenant Approach A)."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making API calls")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    parser.add_argument(
        "--old-config-dir",
        help="Optional directory containing previous tenant configs for removal comparison",
    )
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    configs_with_priority = discover_config_files()

    if not configs_with_priority:
        log.warning("No tenant configuration files found to reconcile.")
        return

    log.info("Discovered %d tenant configuration file(s) in priority order:", len(configs_with_priority))
    for priority, path in configs_with_priority:
        log.info("  -> Priority %3d : %s", priority, path)

    failed_tenants: list[str] = []

    for priority, cfg_path in configs_with_priority:
        log.info("")
        log.info("==========================================================================")
        log.info("RECONCILING TENANT CONFIG [Priority %d]: %s", priority, cfg_path)
        log.info("==========================================================================")

        # Build arguments for setup_pais
        setup_args = ["--config", cfg_path]
        if args.dry_run:
            setup_args.append("--dry-run")
        if args.verbose:
            setup_args.append("--verbose")

        try:
            setup_pais.main(setup_args)
        except SystemExit as sys_exit:
            if sys_exit.code != 0:
                log.error("Setup failed for tenant config '%s' (exit code %s)", cfg_path, sys_exit.code)
                failed_tenants.append(cfg_path)
                continue
        except Exception as exc:
            log.error("Setup raised exception for tenant config '%s': %s", cfg_path, exc)
            failed_tenants.append(cfg_path)
            continue

        # Check for old config version for cleanup
        old_cfg_path = extract_previous_config(cfg_path, old_config_dir=args.old_config_dir)

        if old_cfg_path:
            log.info("Running cleanup for tenant '%s' using old config...", cfg_path)
            cleanup_args = ["--old-config", old_cfg_path, "--new-config", cfg_path]
            if args.dry_run:
                cleanup_args.append("--dry-run")
            if args.verbose:
                cleanup_args.append("--verbose")

            try:
                cleanup_pais.main(cleanup_args)
            except SystemExit as sys_exit:
                if sys_exit.code != 0:
                    log.error("Cleanup failed for tenant config '%s' (exit code %s)", cfg_path, sys_exit.code)
                    failed_tenants.append(cfg_path)
            except Exception as exc:
                log.error("Cleanup raised exception for tenant config '%s': %s", cfg_path, exc)
                failed_tenants.append(cfg_path)
            finally:
                if old_cfg_path and not args.old_config_dir and os.path.exists(old_cfg_path):
                    try:
                        os.remove(old_cfg_path)
                    except Exception:
                        pass
        else:
            log.info("No previous config version found for '%s' - skipping removals (first apply).", cfg_path)

    log.info("")
    log.info("==========================================================================")
    if failed_tenants:
        log.error("Multi-tenant reconciliation finished with ERRORS on %d tenant(s): %s", len(failed_tenants), failed_tenants)
        sys.exit(1)
    else:
        log.info("Multi-tenant reconciliation COMPLETED SUCCESSFULLY for all %d tenant(s).", len(configs_with_priority))


if __name__ == "__main__":
    main()
