# Multi-Tenant GitOps Architecture (Directory-Based Isolation)

This directory contains individual organization/tenant configuration folders under `tenants/<tenant-name>/config.yaml`.

## Active Tenants

1. **`tenants/pais-shared-org/config.yaml`**:
   Manages shared cluster-wide PAIS resources (embeddings model endpoints, shared gateway routes, vector knowledge bases, and MCP tools).

2. **`tenants/pais-all-apps/config.yaml`**:
   Manages application-specific PAIS resources (completion LLMs, gateway routes, application data sources, and autonomous agents).

## Environment Variable & Secret Naming

Each tenant uses explicitly scoped environment variable names for PAIS OIDC authentication, VCF CLI cluster context, and API backend tokens:

### `pais-shared-org`

| Parameter | Environment Variable Name |
| --- | --- |
| PAIS REST Base URL | `PAIS_SHARED_BASE_URL` |
| PAIS OIDC Token URL | `PAIS_SHARED_TOKEN_URL` |
| PAIS Client ID / Secret | `PAIS_SHARED_CLIENT_ID` / `PAIS_SHARED_CLIENT_SECRET` |
| PAIS Username / Password | `PAIS_SHARED_USERNAME` / `PAIS_SHARED_PASSWORD` |
| VCF CLI Endpoint | `VCF_SHARED_ENDPOINT` |
| VCF API Token / Credentials | `VCF_SHARED_API_TOKEN` / `VCF_SHARED_USER` / `VCF_SHARED_PASSWORD` |
| VCF VCFA Namespace & Project | `VCF_SHARED_NAMESPACE` / `VCF_SHARED_PROJECT` |
| Gateway API Backend Token | `SHARED_MODELS_API_TOKEN` |

### `pais-all-apps`

| Parameter | Environment Variable Name |
| --- | --- |
| PAIS REST Base URL | `PAIS_ALL_APPS_BASE_URL` |
| PAIS OIDC Token URL | `PAIS_ALL_APPS_TOKEN_URL` |
| PAIS Client ID / Secret | `PAIS_ALL_APPS_CLIENT_ID` / `PAIS_ALL_APPS_CLIENT_SECRET` |
| PAIS Username / Password | `PAIS_ALL_APPS_USERNAME` / `PAIS_ALL_APPS_PASSWORD` |
| VCF CLI Endpoint | `VCF_ALL_APPS_ENDPOINT` |
| VCF API Token / Credentials | `VCF_ALL_APPS_API_TOKEN` / `VCF_ALL_APPS_USER` / `VCF_ALL_APPS_PASSWORD` |
| VCF VCFA Namespace & Project | `VCF_ALL_APPS_NAMESPACE` / `VCF_ALL_APPS_PROJECT` |
| Gateway API Backend Token | `ALL_APPS_BACKEND_API_TOKEN` |

---

## Directory Structure

```text
api-work/
├── tenants/
│   ├── pais-shared-org/
│   │   └── config.yaml          # Shared embeddings, gateway routes, & vector KBs
│   ├── pais-all-apps/
│   │   └── config.yaml          # Applications PAIS workloads & Agents
│   └── README.md
├── reconcile_all.py             # Orchestrator running setup + cleanup across all tenants
├── setup_pais.py                # Idempotent apply engine
├── cleanup_pais.py              # Git diff-based cleanup engine
└── k8s_manager.py               # VCF CLI context manager & manifest builder
```

## Running Multi-Tenant Reconciliation

### Reconcile All Tenants

```bash
# Dry run across all tenants
python reconcile_all.py --dry-run

# Live apply across all tenants
python reconcile_all.py
```

### Reconcile a Single Tenant

```bash
# Apply Shared Org configuration
python setup_pais.py --config tenants/pais-shared-org/config.yaml

# Apply All-Apps Org configuration
python setup_pais.py --config tenants/pais-all-apps/config.yaml
```
