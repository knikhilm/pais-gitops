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
2. [Authentication & Login Architecture](#2-authentication--login-architecture)
   - [2.1 Kubernetes Cluster Authentication (vSphere / VKS Login)](#21-kubernetes-cluster-authentication-vsphere--vks-login)
   - [2.2 OCI Registry / Harbor Model Pull Authentication](#22-oci-registry--harbor-model-pull-authentication)
   - [2.3 PAIS REST API Data Plane Authentication](#23-pais-rest-api-data-plane-authentication)
3. [Key Capabilities](#3-key-capabilities)
4. [Repository Layout](#4-repository-layout)
5. [Prerequisites](#5-prerequisites)
6. [The Unified Configuration File (`config.yaml`)](#6-the-unified-configuration-file-configyaml)
   - [6.1 Model Endpoint Deployment (`model_endpoints`)](#61-model-endpoint-deployment-model_endpoints)
   - [6.2 Inference Gateway Routing (`inference_routes`)](#62-inference-gateway-routing-inference_routes)
   - [6.3 Data Sources & Knowledge Bases](#63-data-sources--knowledge-bases)
   - [6.4 MCP Servers & Tool Approvals](#64-mcp-servers--tool-approvals)
   - [6.5 Agent & RAG Configuration](#65-agent--rag-configuration)
7. [Model Endpoint Operations & Lifecycle Management](#7-model-endpoint-operations--lifecycle-management)
8. [Secrets and Environment Variables](#8-secrets-and-environment-variables)
9. [Local Execution & Dry Runs](#9-local-execution--dry-runs)
10. [GitHub Secrets & GitOps Pipeline Setup](#10-github-secrets--gitops-pipeline-setup)
11. [End-to-End Walkthrough Example](#11-end-to-end-walkthrough-example)
12. [Reconciliation & Removal Logic](#12-reconciliation--removal-logic)
13. [Troubleshooting](#13-troubleshooting)
14. [CRD & REST API Reference Matrix](#14-crd--rest-api-reference-matrix)

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

## 2. Authentication & Login Architecture

A common question when deploying ModelEndpoints is: **How does the pipeline log in to deploy Kubernetes CRDs and pull model weights?**

The tooling uses a three-part authentication model:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                       Authentication & Login Flow                           │
├─────────────────────────────────────────────────────────────────────────────┤
│ 1. Kubernetes Cluster Login:                                                │
│    - VCF CLI Context (`vcf context create` & `vcf context use` via VCF_*)   │
│    - OR Kubeconfig (`KUBECONFIG_DATA` base64) / Bearer Token (`KUBE_TOKEN`) │
│                                                                             │
│ 2. OCI Model Registry Pull Authentication:                                  │
│    - Automated `kubernetes.io/dockerconfigjson` Secret creation             │
│    - Generated from `HARBOR_REGISTRY`, `HARBOR_USERNAME`, `HARBOR_PASSWORD` │
│    - Referenced by `ModelEndpoint.spec.model.pullSecrets`                   │
│                                                                             │
│ 3. PAIS REST API Data Plane Authentication:                                 │
│    - OIDC Resource Owner Password Flow via `PAIS_TOKEN_URL`, `PAIS_USERNAME`│
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Kubernetes Cluster Authentication (VCF CLI Login)

`k8s_manager.py` authenticates to the VCF Supervisor Cluster / VKS cluster using one of three supported methods:

- **Method A: VCF CLI Context Login (`vcf context create` & `vcf context use`)**  
  Replaces the deprecated `kubectl vsphere login`. When `VCF_ENDPOINT` (or `VSPHERE_SERVER`), `VCF_USER`, and `VCF_PASSWORD` are provided, the script runs:
  ```bash
  vcf context create pais-vcf-context \
    --endpoint=${VCF_ENDPOINT} \
    --username=${VCF_USER} \
    --password=${VCF_PASSWORD} \
    --type vsphere \
    --insecure-skip-tls-verify

  vcf context use pais-vcf-context
  ```

- **Method B: ServiceAccount / Bearer Token Login**  
  When `KUBE_SERVER` and `KUBE_TOKEN` are provided, the script sets up a context:
  ```bash
  kubectl config set-cluster pais-cluster --server=${KUBE_SERVER} --insecure-skip-tls-verify=true
  kubectl config set-credentials pais-sa --token=${KUBE_TOKEN}
  kubectl config set-context pais-context --cluster=pais-cluster --user=pais-sa --namespace=${KUBE_NAMESPACE}
  kubectl config use-context pais-context
  ```

- **Method C: GitHub Actions Kubeconfig Secret (`KUBECONFIG_DATA`)**  
  The workflow decodes `KUBECONFIG_DATA` to `~/.kube/config` before executing python scripts.

### 2.2 OCI Registry / Harbor Model Pull Authentication

Model weights are packaged as OCI artifacts (`ociRef`) in an internal registry like Harbor. For VKS worker nodes to pull these artifacts, a Kubernetes secret of type `kubernetes.io/dockerconfigjson` must exist in the target namespace.

`k8s_manager.py` automatically generates and applies this secret manifest from registry credentials:
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: harbor-registry-secret
  namespace: default
type: kubernetes.io/dockerconfigjson
data:
  .dockerconfigjson: <base64-encoded-docker-auth>
```
`ModelEndpoint` resources reference this secret via `spec.model.pullSecrets: [{ name: "harbor-registry-secret" }]`.

### 2.3 PAIS REST API Data Plane Authentication

For Data Sources, Knowledge Bases, MCP Servers, and Agents, `pais_client.py` uses OpenID Connect (OIDC) Resource Owner Password Flow against the IdP endpoint configured in `PAIS_TOKEN_URL`.

---

## 3. Key Capabilities

- **Automated Model Endpoint Deployment**: Deploy vLLM or Infinity model servers with dedicated vGPU classes (`nvidia-a10g-gpu-class`), vSAN storage, and custom engine parameters (`--gpu-memory-utilization`, `--max-model-len`).
- **Inference Gateway Route Management**: Automatically expose models via Gateway routes or connect to external cloud models smoothly.
- **Automated Login & Pull Secret Management**: Handles cluster authentication (`kubectl vsphere login` or Kubeconfig) and OCI model pull secrets (`harbor-registry-secret`) automatically.
- **RAG & Agent Builder**: Pair deployed embedding models (`Infinity`) with Knowledge Bases and pair deployed LLMs (`vLLM`) with Agents.
- **Idempotent Reconciler**: Re-running pipelines against unchanged configs performs no duplicate creations.
- **Diff-Based Cleanup**: Deleting a `model_endpoint`, `inference_route`, `data_source`, `knowledge_base`, `mcp_server`, or `agent` from `config.yaml` automatically deletes the corresponding resource in the cluster/API in safe dependency order.
- **Artifact Generation**: The pipeline outputs standalone `k8s-manifests/pais-resources.yaml` multi-doc manifests for GitOps review or ArgoCD/Flux sync.

---

## 4. Repository Layout

```
pais-gitops/                           # Repository Root
├── .github/
│   └── workflows/
│       └── pais-gitops.yml            # CI/CD Reconcile Workflow
├── k8s_manager.py                     # K8s Auth, Pull Secrets, CRD Generator & kubectl runner
├── pais_client.py                     # PAIS REST API Client, Auth & Helpers
├── setup_pais.py                      # GitOps Apply Script (CRDs + REST API)
├── cleanup_pais.py                    # GitOps Cleanup Script (Diff-based deletions)
├── config.yaml                        # Desired State Configuration
├── requirements.txt                   # Python Dependencies (httpx, httpx-auth, pyyaml)
├── .gitignore
└── README.md                          # Documentation
```

---

## 5. Prerequisites

1. **A PAIS Kubernetes Cluster**: Access to a VKS / vSphere cluster with PAIS installed (`pais.vmware.com/v1alpha1` CRDs registered).
2. **PAIS OIDC Credentials**: Client ID, Username, Password, Token URL from your PAIS IdP.
3. **vSphere / Cluster Credentials or Kubeconfig**: Credentials to authenticate to the K8s API (`VSPHERE_USER`/`PASS` or `KUBECONFIG_DATA`).
4. **OCI Model Registry Credentials**: Harbor or Docker registry username/password storing model OCI artifacts (`HARBOR_USERNAME`/`PASSWORD`).
5. **Python 3.11+** for local dry runs or manual script execution.

---

## 6. The Unified Configuration File (`config.yaml`)

```yaml
kubernetes:
  namespace: "default"

  # Cluster Login (Optional: if using vSphere SSO login)
  vsphere:
    server: "${VSPHERE_SERVER}"
    username: "${VSPHERE_USER}"
    password: "${VSPHERE_PASSWORD}"
    namespace: "${VSPHERE_NAMESPACE}"

  # Harbor / OCI Registry Secret Provisioning
  registry:
    server: "${HARBOR_REGISTRY}"
    username: "${HARBOR_USERNAME}"
    password: "${HARBOR_PASSWORD}"
    secret_name: "harbor-registry-secret"
```

### 6.1 Model Endpoint Deployment (`model_endpoints`)

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
        - name: "harbor-registry-secret"   # Auto-created by k8s_manager
    inference_server_customization:
      cli_args:
        - "--max-model-len=8192"
        - "--gpu-memory-utilization=0.90"
      env_vars:
        - name: "PAIH_MODEL_ID"
          value: "meta-llama/Meta-Llama-3.1-8B-Instruct"
      shared_memory_mount_size: "2Gi"
```

### 6.2 Inference Gateway Routing (`inference_routes`)

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

### 6.3 Data Sources & Knowledge Bases

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

### 6.4 MCP Servers & Tool Approvals

```yaml
mcp_servers:
  - name: "weather-service"
    url: "https://weather-mcp-server.example.com"
    transport: "STREAMABLE_HTTP"
    approve_tools:
      - "get_current_weather"
      - "get_forecast"
```

### 6.5 Agent & RAG Configuration

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

## 7. Model Endpoint Operations & Lifecycle Management

Common Day-2 operations managed via GitOps:

1. **Scaling Replicas**: Edit `replicas: 1` to `replicas: 3` in `config.yaml` and push.
2. **Upgrading Model Versions**: Update `oci_ref` tag (e.g. `:v1` -> `:v2`) in `config.yaml` and push.
3. **GPU Sizing Tuning**: Modify `virtual_machine_class_name` or `cli_args` (`--gpu-memory-utilization=0.95`).
4. **Decommissioning a Model**: Remove the endpoint and route entries from `config.yaml`. The `cleanup_pais.py` script automatically removes the CRDs from Kubernetes.

---

## 8. Secrets and Environment Variables

Secret interpolation uses `${ENV_VAR_NAME}` syntax:

| Environment Variable | Description |
| --- | --- |
| `PAIS_BASE_URL` | Base URL of PAIS REST Data Plane API |
| `PAIS_TOKEN_URL` | OIDC Token URL |
| `PAIS_CLIENT_ID` | OIDC Client ID |
| `PAIS_USERNAME` | OIDC Admin / User Username |
| `PAIS_PASSWORD` | OIDC Password / Bearer Token |
| `VSPHERE_SERVER` | vSphere Supervisor Cluster FQDN or IP |
| `VSPHERE_USER` | vSphere SSO Username |
| `VSPHERE_PASSWORD` | vSphere SSO Password |
| `VSPHERE_NAMESPACE` | Target vSphere Namespace |
| `HARBOR_REGISTRY` | Harbor / OCI Registry FQDN (e.g. `harbor.internal.example.com`) |
| `HARBOR_USERNAME` | Harbor Registry Username |
| `HARBOR_PASSWORD` | Harbor Registry Password |
| `GDRIVE_CREDENTIALS` | Service Account JSON string for Google Drive |
| `S3_CREDENTIALS` | S3 Access Key / Secret JSON string |
| `KUBECONFIG_DATA` | (Optional) Base64-encoded Kubeconfig for direct `kubectl apply` |

---

## 9. Local Execution & Dry Runs

Run local dry runs to preview CRD generation, pull secrets, and REST API execution without making live changes:

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

## 10. GitHub Secrets & GitOps Pipeline Setup

Add the following Repository Secrets under **Settings ▸ Secrets and variables ▸ Actions**:

```bash
# PAIS REST API Secrets
gh secret set PAIS_BASE_URL      --body "https://pais.example.com"
gh secret set PAIS_TOKEN_URL     --body "https://idp.example.com/realms/pais/protocol/openid-connect/token"
gh secret set PAIS_CLIENT_ID     --body "pais-client"
gh secret set PAIS_USERNAME      --body "admin"
gh secret set PAIS_PASSWORD      --body "your-password"

# vSphere / VCF Cluster Authentication Secrets
gh secret set VCF_ENDPOINT       --body "https://vc.domain.local"
gh secret set VCF_USER           --body "administrator@vsphere.local"
gh secret set VCF_PASSWORD       --body "your-vsphere-password"

# OCI Registry Secrets (for pulling model artifacts)
gh secret set HARBOR_REGISTRY    --body "harbor.internal.example.com"
gh secret set HARBOR_USERNAME    --body "robot$pais-puller"
gh secret set HARBOR_PASSWORD    --body "your-harbor-secret"
```

---

## 11. End-to-End Walkthrough Example

1. **Branch Checkout**:
   ```bash
   git checkout -b 21julyupdates
   ```

2. **Update `config.yaml`**: Define new model endpoints, routes, cluster login parameters, data sources, and agents.

3. **Commit and Push**:
   ```bash
   git add .
   git commit -m "Deploy Llama3 8B vLLM endpoint and support agent"
   git push -u origin 21julyupdates
   ```

4. **GitHub Actions Execution**:
   - Step 0: Authenticates to cluster via `vcf context create` & `vcf context use` (or Kubeconfig), generates `harbor-registry-secret`, builds `pais.vmware.com/v1alpha1` `ModelEndpoint` and `InferenceGatewayRoute` manifests, and applies via `kubectl`.
   - Step 1: Provisions S3 / Google Drive Data Sources.
   - Step 2: Provisions Knowledge Bases and triggers indexing.
   - Step 3-5: Registers MCP Servers and approves tools.
   - Step 6: Provisions Agent linked to Knowledge Base REX search tools and MCP tools.
   - Step 7: Uploads generated `k8s-manifests/pais-resources.yaml` artifact to GitHub Actions summary.

---

## 12. Reconciliation & Removal Logic

- **Ordering**:
  - **Apply Phase**: K8s Cluster Login ➔ Registry Secret Provisioning ➔ K8s CRDs (ModelEndpoints & GatewayRoutes) ➔ Data Sources ➔ Knowledge Bases & Indexes ➔ MCP Servers ➔ Tool Approvals ➔ Agents.
  - **Cleanup Phase**: Agents ➔ Tool Approval Revocation ➔ Knowledge Base Links ➔ Knowledge Bases ➔ MCP Servers ➔ Data Sources ➔ K8s CRDs (GatewayRoutes & ModelEndpoints).
- **CRD Diffing**: Objects are matched by `metadata.name`. Deleting an item from `config.yaml` triggers a targeted `kubectl delete` command.

---

## 13. Troubleshooting

| Issue | Resolution |
| --- | --- |
| `vcf context create` fails | Ensure `VCF_ENDPOINT` (or `VSPHERE_SERVER`), `VCF_USER`, and `VCF_PASSWORD` secrets are correct, and the `vcf` CLI is installed on the runner. |
| ModelEndpoint status `ImagePullBackOff` | Verify `harbor-registry-secret` creation. Ensure `HARBOR_REGISTRY`, `HARBOR_USERNAME`, and `HARBOR_PASSWORD` are valid and the user has pull permissions on the OCI repository. |
| ModelEndpoint status `Pending` | Check node pool vGPU availability (`virtualMachineClassName`) or vSphere Zone (`failureDomain`). |
| Agent return code 404 on Model | Verify that the `routing_name` in `ModelEndpoint` matches the `matches.routing_name` in `InferenceGatewayRoute`. |

---

## 14. CRD & REST API Reference Matrix

| Capability | Resource Kind / API Path | API Group / Endpoint |
| --- | --- | --- |
| Cluster Login | `kubectl vsphere login` | vSphere Supervisor SSO |
| Registry Pull Secret | `Secret` (`dockerconfigjson`) | `core/v1` |
| Model Endpoint | `ModelEndpoint` | `pais.vmware.com/v1alpha1` |
| Gateway Routing | `InferenceGatewayRoute` | `pais.vmware.com/v1alpha1` |
| Data Source | REST Data Source | `/api/v1/control/data-sources` |
| Knowledge Base | REST Knowledge Base | `/api/v1/control/knowledge-bases` |
| Index & Search | REST Index & REX Tool | `/api/v1/control/knowledge-bases/{id}/indexes` |
| MCP Server | REST MCP Server | `/api/v1/control/mcp-servers` |
| Agent Builder | REST Agent | `/api/v1/compatibility/openai/v1/agents` |
