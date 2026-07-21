# PAIS GitOps Tooling & Model Lifecycle Automation

Declaratively deploy and manage **VMware Private AI Services (PAIS)** resources —
from **Kubernetes ModelEndpoint deployments** and **InferenceGatewayRoutes** down to
**Data Sources, Knowledge Bases, MCP Tool integrations, and AI Agents** — from a single
unified YAML configuration file, reconciled automatically via GitHub Actions.

- Kubernetes CRD API Reference: <https://developer.broadcom.com/xapis/vmware-private-ai-service-kubernetes-api/latest/api-docs.html>
- REST Data & Agent Plane API Reference: <https://developer.broadcom.com/xapis/vmware-private-ai-service-api/latest/>

---

## Table of Contents

1. [Architectural Overview](#1-architectural-overview)
2. [Key Capabilities](#2-key-capabilities)
3. [Repository Layout](#3-repository-layout)
4. [Prerequisites](#4-prerequisites)
5. [The Unified Configuration File (`config.yaml`)](#5-the-unified-configuration-file-configyaml)
   - [5.1 Model Endpoint Deployment (`model_endpoints`)](#51-model-endpoint-deployment-model_endpoints)
   - [5.2 Inference Gateway Routing (`inference_routes`)](#52-inference-gateway-routing-inference_routes)
   - [5.3 Data Sources & Knowledge Bases](#53-data-sources--knowledge-bases)
   - [5.4 MCP Servers & Tool Approvals](#54-mcp-servers--tool-approvals)
   - [5.5 Agent & RAG Configuration](#55-agent--rag-configuration)
6. [Model Endpoint Operations & Lifecycle Management](#6-model-endpoint-operations--lifecycle-management)
7. [Secrets and Environment Variables](#7-secrets-and-environment-variables)
8. [Local Execution & Dry Runs](#8-local-execution--dry-runs)
9. [GitHub Secrets & GitOps Pipeline Setup](#9-github-secrets--gitops-pipeline-setup)
10. [End-to-End Walkthrough Example](#10-end-to-end-walkthrough-example)
11. [Reconciliation & Removal Logic](#11-reconciliation--removal-logic)
12. [Troubleshooting](#12-troubleshooting)
13. [CRD & REST API Reference Matrix](#13-crd--rest-api-reference-matrix)

---

## 1. Architectural Overview

VMware Private AI Services (PAIS) operates across two primary planes:

1. **Control & Compute Plane (Kubernetes CRDs - `pais.vmware.com/v1alpha1`)**:
   - **`ModelEndpoint`**: Serves AI models (LLMs/Embeddings) on vSphere / VKS node pools using engines like **vLLM**, **Infinity**, or **LlamaCPP**. Customizes GPU classes (`virtualMachineClassName`), storage classes, OCI registry model references (`ociRef`), replicas, CLI flags, and shared memory sizes.
   - **`InferenceGatewayRoute`**: Defines routing rules mapping client requests (by `routingName`) to backend ModelEndpoints, cross-namespace PAIS ingress services, or external cloud LLMs (OpenAI, Anthropic).

2. **Data & Agent Plane (REST APIs)**:
   - **Data Sources & Knowledge Bases**: Ingests document stores (Google Drive, S3, SharePoint) and splits/embeds them into vector indexes.
   - **MCP Tool Integration**: Connects external Model Context Protocol (MCP) servers and approves specific tools.
   - **Agents**: RAG-enabled agents combining Knowledge Base search (REX tools) and external MCP tools.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Unified GitOps (config.yaml)                       │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ Push / Reconcile
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                     GitHub Actions Pipeline (pais-gitops.yml)               │
│                                                                             │
│  Step 0: k8s_manager.py         Step 1-6: setup_pais.py / cleanup_pais.py   │
└──────────────────┬──────────────────────────────────┬───────────────────────┘
                   │                                  │
                   │ kubectl apply / CRD Manifests     │ OIDC Bearer REST API
                   ▼                                  ▼
┌──────────────────────────────────────┐  ┌───────────────────────────────────┐
│     Kubernetes Control Plane         │  │       PAIS Data Plane REST API    │
│  (pais.vmware.com/v1alpha1)          │  │                                   │
│  ├── ModelEndpoints (vLLM/Infinity) │  │  ├── Data Sources & KBs           │
│  └── InferenceGatewayRoutes          │  │  ├── MCP Servers & Tool Approvals │
└──────────────────────────────────────┘  │  └── RAG Agents & REX Tools       │
                                          └───────────────────────────────────┘
```

---

## 2. Key Capabilities

- **Automated Model Endpoint Deployment**: Deploy vLLM or Infinity model servers with dedicated vGPU classes (`nvidia-a10g-gpu-class`), vSAN storage, and custom engine parameters (`--gpu-memory-utilization`, `--max-model-len`).
- **Inference Gateway Route Management**: Automatically expose models via Gateway routes or connect to external cloud models smoothly.
- **RAG & Agent Builder**: Pair deployed embedding models (`Infinity`) with Knowledge Bases and pair deployed LLMs (`vLLM`) with Agents.
- **Idempotent Reconciler**: Re-running pipelines against unchanged configs performs no duplicate creations.
- **Diff-Based Cleanup**: Deleting a `model_endpoint`, `inference_route`, `data_source`, `knowledge_base`, `mcp_server`, or `agent` from `config.yaml` automatically deletes the corresponding resource in the cluster/API in safe dependency order.
- **Artifact Generation**: The pipeline outputs standalone `k8s-manifests/pais-resources.yaml` multi-doc manifests for GitOps review or ArgoCD/Flux sync.

---

## 3. Repository Layout

```
pais-gitops/                           # Repository Root
├── .github/
│   └── workflows/
│       └── pais-gitops.yml            # CI/CD Reconcile Workflow
├── k8s_manager.py                     # Kubernetes CRD Generator & kubectl runner
├── pais_client.py                     # PAIS REST API Client, Auth & Helpers
├── setup_pais.py                      # GitOps Apply Script (CRDs + REST API)
├── cleanup_pais.py                    # GitOps Cleanup Script (Diff-based deletions)
├── config.yaml                        # Desired State Configuration
├── requirements.txt                   # Python Dependencies (httpx, httpx-auth, pyyaml)
├── .gitignore
└── README.md                          # Documentation
```

---

## 4. Prerequisites

1. **A PAIS Kubernetes Cluster**: Access to a VKS / vSphere cluster with PAIS installed (`pais.vmware.com/v1alpha1` CRDs registered).
2. **PAIS OIDC Credentials**: Client ID, Username, Password, Token URL from your PAIS IdP.
3. **OCI Model Registry**: Harbor or Docker registry storing model OCI artifacts (`ociRef`).
4. **Python 3.11+** for local dry runs or manual script execution.
5. **kubectl** (optional for local apply, used automatically in CI if `KUBECONFIG_DATA` secret is provided).

---

## 5. The Unified Configuration File (`config.yaml`)

### 5.1 Model Endpoint Deployment (`model_endpoints`)

`ModelEndpoint` defines how an AI model is served on Kubernetes node pools:

```yaml
model_endpoints:
  - name: "llama3-8b-endpoint"
    namespace: "default"
    type: "Completions"                     # Completions | Embeddings
    engine: "vLLM"                          # vLLM | Infinity | LlamaCPP
    routing_name: "meta-llama/Meta-Llama-3.1-8B-Instruct"
    replicas: 1
    virtual_machine_class_name: "nvidia-a10g-gpu-class"
    storage_class_name: "vsan-default-storage-class"
    failure_domain: "zone-1"                # vSphere Zone (optional)
    model:
      oci_ref: "harbor.internal.example.com/pais/models/meta-llama-3.1-8b-instruct:v1"
      pull_secrets:
        - name: "harbor-registry-secret"
    inference_server_customization:
      cli_args:
        - "--max-model-len=8192"
        - "--gpu-memory-utilization=0.90"
      env_vars:
        - name: "PAIH_MODEL_ID"
          value: "meta-llama/Meta-Llama-3.1-8B-Instruct"
      shared_memory_mount_size: "2Gi"
```

### 5.2 Inference Gateway Routing (`inference_routes`)

`InferenceGatewayRoute` maps API client requests to local `ModelEndpoint` services or external providers:

```yaml
inference_routes:
  - name: "route-llama3-8b"
    namespace: "default"
    type: "Completions"                     # Completions | Embeddings
    engine: "vLLM"                          # vLLM | Infinity | LlamaCPP | OpenAI
    matches:
      routing_name: "meta-llama/Meta-Llama-3.1-8B-Instruct"
    backend:
      http_base_url: "http://llama3-8b-endpoint.default.svc.cluster.local"
      model_id: "meta-llama/Meta-Llama-3.1-8B-Instruct"
      tls:
        verification: "strict"              # strict | caOnly | none | mutual
```

### 5.3 Data Sources & Knowledge Bases

```yaml
data_sources:
  - name: "gdrive-product-docs"
    type: "GOOGLE_DRIVE"
    origin_url: "https://drive.google.com/drive/u/0/folders/abc123"
    credentials: "${GDRIVE_CREDENTIALS}"
    test_connection: true

knowledge_bases:
  - name: "product-docs-kb"
    data_origin_type: "DATA_SOURCES"
    index_refresh_policy: { policy_type: "MANUAL" }
    data_sources: [ "gdrive-product-docs" ]
    index:
      name: "product-docs-index"
      embeddings_model_endpoint: "BAAI/bge-small-en-v1.5" # References deployed embedding route
      text_splitting: "SENTENCE"
      chunk_size: 100
      chunk_overlap: 0
      trigger_indexing: true
      wait_for_indexing: true
```

### 5.4 MCP Servers & Tool Approvals

```yaml
mcp_servers:
  - name: "weather-service"
    url: "https://weather-mcp-server.example.com"
    transport: "STREAMABLE_HTTP"
    approve_tools:
      - "get_current_weather"
      - "get_forecast"
```

### 5.5 Agent & RAG Configuration

```yaml
agents:
  - name: "product-support-agent"
    model: "meta-llama/Meta-Llama-3.1-8B-Instruct" # References deployed completions route
    instructions: "You are a helpful product support assistant."
    knowledge_bases:
      - name: "product-docs-kb"
        top_n: 5
        similarity_cutoff: 0.65
    mcp_tools:
      - server: "weather-service"
        tool_name: "get_current_weather"
```

---

## 6. Model Endpoint Operations & Lifecycle Management

Common Day-2 operations managed via GitOps:

1. **Scaling Replicas**: Edit `replicas: 1` to `replicas: 3` in `config.yaml` and push.
2. **Upgrading Model Versions**: Update `oci_ref` tag (e.g. `:v1` -> `:v2`) in `config.yaml` and push.
3. **GPU Sizing Tuning**: Modify `virtual_machine_class_name` or `cli_args` (`--gpu-memory-utilization=0.95`).
4. **Decommissioning a Model**: Remove the endpoint and route entries from `config.yaml`. The `cleanup_pais.py` script automatically removes the CRDs from Kubernetes.

---

## 7. Secrets and Environment Variables

Secret interpolation uses `${ENV_VAR_NAME}` syntax:

| Environment Variable | Description |
| --- | --- |
| `PAIS_BASE_URL` | Base URL of PAIS REST Data Plane API |
| `PAIS_TOKEN_URL` | OIDC Token URL |
| `PAIS_CLIENT_ID` | OIDC Client ID |
| `PAIS_USERNAME` | OIDC Admin / User Username |
| `PAIS_PASSWORD` | OIDC Password / Bearer Token |
| `GDRIVE_CREDENTIALS` | Service Account JSON string for Google Drive |
| `S3_CREDENTIALS` | S3 Access Key / Secret JSON string |
| `KUBECONFIG_DATA` | (Optional) Base64-encoded Kubeconfig for direct `kubectl apply` |

---

## 8. Local Execution & Dry Runs

Run local dry runs to preview CRD generation and REST API execution without making live changes:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Preview additions & updates (Generates k8s-manifests/pais-resources.yaml)
python setup_pais.py --config config.yaml --dry-run --verbose

# 3. Inspect generated Kubernetes manifests
cat k8s-manifests/pais-resources.yaml

# 4. Preview removals
python cleanup_pais.py --old-config previous_config.yaml --new-config config.yaml --dry-run
```

---

## 9. GitHub Secrets & GitOps Pipeline Setup

Add the following Repository Secrets under **Settings ▸ Secrets and variables ▸ Actions**:

```bash
gh secret set PAIS_BASE_URL      --body "https://pais.example.com"
gh secret set PAIS_TOKEN_URL     --body "https://idp.example.com/realms/pais/protocol/openid-connect/token"
gh secret set PAIS_CLIENT_ID     --body "pais-client"
gh secret set PAIS_USERNAME      --body "admin"
gh secret set PAIS_PASSWORD      --body "your-password"
gh secret set GDRIVE_CREDENTIALS --body '{"type":"service_account",...}'
gh secret set KUBECONFIG_DATA    --body "$(cat ~/.kube/config | base64 -w 0)"
```

The pipeline automatically runs on pushes to `main` or `21julyupdates` branches.

---

## 10. End-to-End Walkthrough Example

1. **Branch Checkout**:
   ```bash
   git checkout -b 21julyupdates
   ```

2. **Update `config.yaml`**: Define new model endpoints, routes, data sources, and agents.

3. **Commit and Push**:
   ```bash
   git add .
   git commit -m "Deploy Llama3 8B vLLM endpoint and support agent"
   git push -u origin 21julyupdates
   ```

4. **GitHub Actions Execution**:
   - Step 0: Generates `pais.vmware.com/v1alpha1` `ModelEndpoint` and `InferenceGatewayRoute` manifests and applies via `kubectl`.
   - Step 1: Provisions S3 / Google Drive Data Sources.
   - Step 2: Provisions Knowledge Bases and triggers indexing.
   - Step 3-5: Registers MCP Servers and approves tools.
   - Step 6: Provisions Agent linked to Knowledge Base REX search tools and MCP tools.
   - Step 7: Uploads generated `k8s-manifests/pais-resources.yaml` artifact to GitHub Actions summary.

---

## 11. Reconciliation & Removal Logic

- **Ordering**:
  - **Apply Phase**: K8s CRDs (ModelEndpoints & GatewayRoutes) ➔ Data Sources ➔ Knowledge Bases & Indexes ➔ MCP Servers ➔ Tool Approvals ➔ Agents.
  - **Cleanup Phase**: Agents ➔ Tool Approval Revocation ➔ Knowledge Base Links ➔ Knowledge Bases ➔ MCP Servers ➔ Data Sources ➔ K8s CRDs (GatewayRoutes & ModelEndpoints).
- **CRD Diffing**: Objects are matched by `metadata.name`. Deleting an item from `config.yaml` triggers a targeted `kubectl delete` command.

---

## 12. Troubleshooting

| Issue | Resolution |
| --- | --- |
| `kubectl: command not found` in CI | Supply `KUBECONFIG_DATA` secret in GitHub secrets to enable cluster interaction. Otherwise, inspect the uploaded `k8s-manifests/pais-resources.yaml` build artifact. |
| ModelEndpoint status `Pending` | Check node pool vGPU availability (`virtualMachineClassName`) or vSphere Zone (`failureDomain`). |
| OCI Image Pull Error | Ensure `pullSecrets` matches a valid Kubernetes secret containing registry credentials for Harbor/Docker. |
| Agent return code 404 on Model | Verify that the `routing_name` in `ModelEndpoint` matches the `matches.routing_name` in `InferenceGatewayRoute`. |

---

## 13. CRD & REST API Reference Matrix

| Capability | Resource Kind / API Path | API Group / Endpoint |
| --- | --- | --- |
| Model Endpoint | `ModelEndpoint` | `pais.vmware.com/v1alpha1` |
| Gateway Routing | `InferenceGatewayRoute` | `pais.vmware.com/v1alpha1` |
| Data Source | REST Data Source | `/api/v1/control/data-sources` |
| Knowledge Base | REST Knowledge Base | `/api/v1/control/knowledge-bases` |
| Index & Search | REST Index & REX Tool | `/api/v1/control/knowledge-bases/{id}/indexes` |
| MCP Server | REST MCP Server | `/api/v1/control/mcp-servers` |
| Agent Builder | REST Agent | `/api/v1/compatibility/openai/v1/agents` |
