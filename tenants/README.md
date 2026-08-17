# Multi-Tenant GitOps Architecture (Directory-Based Isolation)

This directory contains individual organization/tenant configuration folders under `tenants/<tenant-name>/config.yaml`.

## Overview

The **Directory-Based Multi-Tenancy (Approach A)** model provides strict, declarative isolation across multiple VCFA namespaces, projects, or organization boundaries while maintaining a single, automated GitOps CI/CD pipeline.

### Directory Structure

```text
api-work/
├── tenants/
│   ├── finance-org/
│   │   └── config.yaml          # Finance VCFA namespace & AI workloads
│   ├── hr-org/
│   │   └── config.yaml          # HR VCFA namespace & AI workloads
│   ├── shared-services/
│   │   └── config.yaml          # Shared embeddings, gateway routes, & vector KBs
│   └── README.md
├── reconcile_all.py             # Orchestrator running setup + cleanup across all tenants
├── setup_pais.py                # Idempotent apply engine
├── cleanup_pais.py              # Git diff-based cleanup engine
└── k8s_manager.py               # VCF CLI context manager & manifest builder
```

## Key Benefits

1. **Namespace & RBAC Isolation:**
   Each tenant's `config.yaml` explicitly defines its own VCFA namespace (`namespace`), project (`project_name`), and context (`context_name`). During execution, `k8s_manager.py` switches to that tenant's VCF context (`vcf context use`), preventing cross-tenant resource bleed.

2. **Access Control (CODEOWNERS):**
   GitHub `CODEOWNERS` can be configured to grant specific teams (e.g., `@org/finance-team`) approval rights exclusively over `tenants/finance-org/`.

3. **Autonomous CI/CD Reconciliation:**
   The GitHub Actions workflow automatically discovers all tenant config files (`tenants/*/config.yaml`), executes `setup_pais.py` for additions/updates, and executes `cleanup_pais.py` comparing against previous commits for teardowns.

4. **Multi-Tenant Manifest Artifacts:**
   Generated Kubernetes Custom Resource manifests are written to tenant-specific artifact files (e.g., `k8s-manifests/finance-org-pais-resources.yaml`).

## Running Multi-Tenant Reconciliation

### 1. Reconcile All Tenants (Local or CI)

To reconcile all tenant configurations sequentially:

```bash
# Dry run across all tenants
python reconcile_all.py --dry-run

# Execute live apply across all tenants
python reconcile_all.py
```

### 2. Reconcile a Single Tenant

To target a single tenant directly:

```bash
# Apply Finance Org configuration
python setup_pais.py --config tenants/finance-org/config.yaml

# Clean up removed resources for Finance Org
python cleanup_pais.py --old-config /tmp/prev_finance.yaml --new-config tenants/finance-org/config.yaml
```
