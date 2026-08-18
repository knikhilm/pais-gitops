"""
VCF Private AI Services (PAIS) - Multi-Tenant Reconciliation Orchestrator
========================================================================
Discovers and reconciles all tenant configurations (under `tenants/*/config.yaml`
and root `config.yaml` if present) sequentially.

Usage:
  python reconcile_all.py [--dry-run] [--verbose] [--old-config-dir <dir>]
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import sys

import cleanup_pais
import setup_pais
from pais_client import log, setup_logging


def discover_config_files() -> list[str]:
    """Discover all active tenant config.yaml files."""
    configs: list[str] = []

    # 1. Root config.yaml if present
    if os.path.exists("config.yaml"):
        configs.append("config.yaml")

    # 2. Tenants config files (tenants/*/config.yaml)
    tenant_matches = sorted(glob.glob("tenants/*/config.yaml"))
    for match in tenant_matches:
        norm_match = match.replace("\\", "/")
        if norm_match not in configs:
            configs.append(norm_match)

    return configs


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile all tenant PAIS configurations (Multi-Tenant Approach A)."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print actions without making API calls")
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging")
    parser.add_argument(
        "--old-config-dir",
        help="Optional directory containing previous tenant configs for removal comparison",
    )
    args = parser.parse_args(argv)

    setup_logging(args.verbose)
    configs = discover_config_files()

    if not configs:
        log.warning("No tenant configuration files found to reconcile.")
        return

    log.info("Discovered %d tenant configuration file(s): %s", len(configs), configs)

    failed_tenants: list[str] = []

    for cfg_path in configs:
        log.info("")
        log.info("==========================================================================")
        log.info("RECONCILING TENANT CONFIG: %s", cfg_path)
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

        # Check for old config for cleanup
        old_cfg_path = None
        if args.old_config_dir:
            norm = cfg_path.replace("\\", "/").strip("/")
            tenant_name = norm.split("/")[-2] if "/" in norm else "root"
            candidate = os.path.join(args.old_config_dir, f"{tenant_name}_old.yaml")
            if os.path.exists(candidate):
                old_cfg_path = candidate

        if old_cfg_path:
            log.info("Running cleanup for tenant '%s' using old config '%s'...", cfg_path, old_cfg_path)
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

    log.info("")
    log.info("==========================================================================")
    if failed_tenants:
        log.error("Multi-tenant reconciliation finished with ERRORS on %d tenant(s): %s", len(failed_tenants), failed_tenants)
        sys.exit(1)
    else:
        log.info("Multi-tenant reconciliation COMPLETED SUCCESSFULLY for all %d tenant(s).", len(configs))


if __name__ == "__main__":
    main()
