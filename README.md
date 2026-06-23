# PAIS GitOps Tooling

Declaratively manage **VMware Private AI Services (PAIS)** resources — data
sources, knowledge bases, indexes, MCP servers, tool approvals, and agents —
from a single YAML file, with additions and removals reconciled automatically
through GitHub Actions.

- API reference: <https://developer.broadcom.com/xapis/vmware-private-ai-service-api/latest/>

---

## Table of contents

1. [How it works](#1-how-it-works)
2. [Repository layout](#2-repository-layout)
3. [Prerequisites](#3-prerequisites)
4. [The config file explained](#4-the-config-file-explained)
5. [Secrets and environment variables](#5-secrets-and-environment-variables)
6. [Running locally](#6-running-locally)
7. [Creating the GitHub secrets](#7-creating-the-github-secrets)
8. [The GitHub Actions workflow](#8-the-github-actions-workflow)
9. [End-to-end example](#9-end-to-end-example)
10. [How additions and removals are reconciled](#10-how-additions-and-removals-are-reconciled)
11. [Troubleshooting](#11-troubleshooting)
12. [API endpoint reference](#12-api-endpoint-reference)

---

## 1. How it works

Your desired state lives in [`config.yaml`](./config.yaml). Two scripts reconcile
that desired state against a live PAIS instance:

| Script | Role | Trigger |
| --- | --- | --- |
| `setup_pais.py` | **Apply** — create missing objects, update existing ones (idempotent) | Every run |
| `cleanup_pais.py` | **Remove** — delete objects that were taken *out of* the config since the last commit | When a previous config version is available |

A shared module, `pais_client.py`, holds the HTTP client, pagination,
`${ENV_VAR}` interpolation, and the API path constants used by both scripts.

```
┌─────────────┐     edit & push      ┌──────────────────────┐
│ config.yaml │ ───────────────────► │  GitHub Actions       │
└─────────────┘                      │  (pais-gitops.yml)    │
                                     │                       │
                                     │  1) setup_pais.py     │  create/update
                                     │  2) cleanup_pais.py   │  delete removed
                                     └───────────┬───────────┘
                                                 │ HTTPS + OIDC token
                                                 ▼
                                       ┌────────────────────┐
                                       │   PAIS instance     │
                                       └────────────────────┘
```

The **provisioning order** (and the reverse **deletion order**) respects
dependencies so the API never rejects an operation on an in-use object:

- Apply: data sources → knowledge bases (+ links + indexes) → MCP servers →
  tool approvals → REX tool discovery → agents.
- Delete: agents → revoke tool approvals → unlink data sources → knowledge
  bases → MCP servers → data sources.

---

## 2. Repository layout

The repository root contains the config, the scripts, and the workflow
directly (this `api-work` folder *is* the repo `vcf/pais-gitops`):

```
pais-gitops/                       # repository root
├── .github/
│   └── workflows/
│       └── pais-gitops.yml        # CI workflow
├── config.yaml                    # desired state (edit this)
├── pais_client.py                 # shared client + helpers
├── setup_pais.py                  # apply additions/updates
├── cleanup_pais.py                # apply removals (diff-based)
├── requirements.txt               # Python dependencies
├── .gitignore
└── README.md                      # this file
```

> **Note on the repo root.** The workflow expects `config.yaml` and the scripts
> to live at the repository root. If you nest them under a subfolder, update the
> `paths:` filter and add a matching `working-directory:` in `pais-gitops.yml`.

---

## 3. Prerequisites

- **A reachable PAIS instance** with an embedding model and a completion/LLM
  model already deployed (e.g. `BAAI/bge-small-en-v1.5` and
  `meta-llama/Meta-Llama-3.1-8B-Instruct`).
- **OIDC credentials** that support the *Resource Owner Password* grant
  (client id, username/password, token endpoint, scope). These are obtained
  from the IdP configured for your PAIS instance — see your instance's
  `https://<fqdn>/env.json`.
- **Python 3.11+** for local runs.
- **A GitHub repository** with Actions enabled (for the CI flow).
- Any **data-source credentials** you intend to use (e.g. a Google service
  account JSON, AWS keys), and reachable **external MCP server URLs**.

---

## 4. The config file explained

`config.yaml` has five top-level sections. Cross-references are **by `name`**,
so names must be unique and stable within the file.

### 4.1 `pais` — connection and auth

```yaml
pais:
  base_url: "${PAIS_BASE_URL}"            # e.g. https://pais.local
  auth:
    token_url: "${PAIS_TOKEN_URL}"
    client_id: "${PAIS_CLIENT_ID}"
    scope: "openid"
    username: "${PAIS_USERNAME}"
    password: "${PAIS_PASSWORD}"
    verify_ssl: true                      # set false ONLY for dev/self-signed
  rex_discovery_timeout_seconds: 30       # wait for built-in REX tools to appear
```

Any `${VAR}` is replaced at runtime from the environment. An environment
variable of the same canonical name also overrides the value directly (see
[section 5](#5-secrets-and-environment-variables)).

### 4.2 `data_sources`

```yaml
data_sources:
  - name: "gdrive-product-docs"          # unique key used by knowledge_bases
    description: "Product docs in Google Drive"
    type: "GOOGLE_DRIVE"                  # GOOGLE_DRIVE | S3 | SHAREPOINT | WEB | LOCAL
    origin_url: "https://drive.google.com/drive/u/0/folders/<FOLDER_ID>"
    credentials: "${GDRIVE_CREDENTIALS}"  # JSON string injected from env/secret
    test_connection: true                 # validate connectivity before creating
```

`credentials` must be a JSON **string**. Keep it out of git by referencing an
env var/secret that contains the single-line JSON.

### 4.3 `knowledge_bases`

```yaml
knowledge_bases:
  - name: "product-docs-kb"
    description: "KB for product documentation"
    data_origin_type: "DATA_SOURCES"
    index_refresh_policy:
      policy_type: "MANUAL"               # MANUAL | SCHEDULED
    data_sources:                         # names from the data_sources section
      - "gdrive-product-docs"
    index:
      name: "product-docs-index"
      embeddings_model_endpoint: "BAAI/bge-small-en-v1.5"
      text_splitting: "SENTENCE"          # SENTENCE | FIXED
      chunk_size: 100
      chunk_overlap: 0
      trigger_indexing: true              # run an indexing on FIRST creation
      wait_for_indexing: true             # block until DONE/FAILED
      indexing_timeout_seconds: 300
```

When the index is first created, PAIS automatically registers a built-in **REX
search tool** named `search_<index-id-hex>`. The apply script discovers this
tool and wires it into any agent that references the knowledge base.

> `trigger_indexing` only fires when the index is newly created, so re-running
> apply on an unchanged config will **not** repeatedly re-index.

### 4.4 `mcp_servers`

```yaml
mcp_servers:
  - name: "weather-service"
    description: "Real-time weather MCP server"
    url: "https://weather-mcp-server.example.com"
    transport: "STREAMABLE_HTTP"          # STREAMABLE_HTTP | SSE
    approve_tools:                         # tool names to approve for use
      - "get_current_weather"
      - "get_forecast"
```

Tools from external MCP servers are **unapproved by default**; only the names
listed in `approve_tools` are approved. Removing a name later **revokes** that
approval (see [section 10](#10-how-additions-and-removals-are-reconciled)).

### 4.5 `agents`

```yaml
agents:
  - name: "product-support-agent"
    description: "Support agent with docs + live tools"
    model: "meta-llama/Meta-Llama-3.1-8B-Instruct"
    instructions: "You are a helpful product-support assistant."
    completion_role: "assistant"
    session_max_length: 10000
    session_summarization_strategy: "delete_oldest"   # delete_oldest | summarize
    chat_system_instruction_mode: "system-message"
    index_reference_format: "structured"               # structured | plain | null
    knowledge_bases:                       # REX search tool links
      - name: "product-docs-kb"
        top_n: 5
        similarity_cutoff: 0.65
    mcp_tools:                             # external MCP tool links
      - server: "weather-service"
        tool_name: "get_current_weather"
```

- `knowledge_bases[]` become `PAIS_KNOWLEDGE_BASE_INDEX_SEARCH_TOOL_LINK`
  entries with optional `top_n` / `similarity_cutoff`.
- `mcp_tools[]` become `GENERIC_MCP_TOOL_LINK` entries (the referenced tool must
  appear in some server's `approve_tools`).

---

## 5. Secrets and environment variables

Both scripts resolve connection settings from the environment first, then fall
back to `config.yaml`. The following variables are recognized:

| Variable | Purpose | Required |
| --- | --- | --- |
| `PAIS_BASE_URL` | PAIS instance base URL | Yes |
| `PAIS_TOKEN_URL` | OIDC token endpoint | Yes |
| `PAIS_CLIENT_ID` | OIDC client id | Yes |
| `PAIS_USERNAME` | OIDC username | Yes |
| `PAIS_PASSWORD` | OIDC password / API token | Yes |
| `PAIS_SCOPE` | OIDC scope (default `openid`) | No |
| `PAIS_VERIFY_SSL` | `false` to disable TLS verification | No |
| `GDRIVE_CREDENTIALS` | JSON string referenced by a data source | Only if used |
| `S3_CREDENTIALS` | JSON string referenced by a data source | Only if used |

In addition, **any** `${VAR}` placeholder anywhere in `config.yaml` is expanded
from the environment, so you can introduce your own (e.g.
`${SHAREPOINT_CREDENTIALS}`) and add a matching secret.

---

## 6. Running locally

```bash
cd api-work
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

pip install -r requirements.txt
```

Export the connection settings (PowerShell example):

```powershell
$env:PAIS_BASE_URL   = "https://pais.local"
$env:PAIS_TOKEN_URL  = "https://idp.local/realms/pais/protocol/openid-connect/token"
$env:PAIS_CLIENT_ID  = "pais-client"
$env:PAIS_USERNAME   = "admin"
$env:PAIS_PASSWORD   = "********"
$env:GDRIVE_CREDENTIALS = (Get-Content .\gdrive-sa.json -Raw)
```

Preview everything without touching the API:

```bash
python setup_pais.py --config config.yaml --dry-run --verbose
```

Apply for real:

```bash
python setup_pais.py --config config.yaml
```

Preview removals by comparing two config versions:

```bash
python cleanup_pais.py --old-config previous-config.yaml --new-config config.yaml --dry-run
```

> `--dry-run` makes **no** network calls and prints exactly what would happen.
> Use it liberally before applying.

---

## 7. Creating the GitHub secrets

The workflow reads every credential from **repository secrets**. Create them
either through the web UI or the GitHub CLI.

### 7.1 Using the GitHub web UI

1. Push this project to a GitHub repository (see [section 8.1](#81-first-push)).
2. In the repository, go to **Settings** ▸ **Secrets and variables** ▸
   **Actions**.
3. Click **New repository secret**.
4. Add each secret below — **Name** must match exactly, **Secret** is the value:

   | Name | Example value |
   | --- | --- |
   | `PAIS_BASE_URL` | `https://pais.local` |
   | `PAIS_TOKEN_URL` | `https://idp.local/realms/pais/protocol/openid-connect/token` |
   | `PAIS_CLIENT_ID` | `pais-client` |
   | `PAIS_USERNAME` | `admin` |
   | `PAIS_PASSWORD` | `your-password-or-api-token` |
   | `PAIS_SCOPE` | `openid` |
   | `GDRIVE_CREDENTIALS` | *(the entire service-account JSON, single line)* |
   | `S3_CREDENTIALS` | `{"aws_access_key_id":"...","aws_secret_access_key":"...","region_name":"us-east-1"}` |

5. Click **Add secret** for each. Only create `GDRIVE_CREDENTIALS` /
   `S3_CREDENTIALS` (or your own) if those data sources are used.

> For a JSON credential, paste it as a single line. The value is stored
> verbatim and injected wherever `config.yaml` references it via `${...}`.

### 7.2 Using the GitHub CLI

Install and authenticate the [GitHub CLI](https://cli.github.com/) (`gh auth
login`), then from the repository root:

```bash
# Simple string secrets
gh secret set PAIS_BASE_URL  --body "https://pais.local"
gh secret set PAIS_TOKEN_URL --body "https://idp.local/realms/pais/protocol/openid-connect/token"
gh secret set PAIS_CLIENT_ID --body "pais-client"
gh secret set PAIS_USERNAME  --body "admin"
gh secret set PAIS_SCOPE     --body "openid"

# Prompt without echoing to the terminal (recommended for passwords)
gh secret set PAIS_PASSWORD

# File-based JSON credential (reads the whole file as the secret value)
gh secret set GDRIVE_CREDENTIALS < ./gdrive-sa.json
gh secret set S3_CREDENTIALS     --body '{"aws_access_key_id":"AKIA...","aws_secret_access_key":"...","region_name":"us-east-1"}'
```

Verify they exist (names only — values are never shown):

```bash
gh secret list
```

### 7.3 Optional: environment-scoped secrets

For an approval gate before changes hit PAIS, create a GitHub **Environment**
(Settings ▸ Environments, e.g. `production`), attach the secrets there, add
required reviewers, and add `environment: production` to the `reconcile` job in
the workflow.

---

## 8. The GitHub Actions workflow

`.github/workflows/pais-gitops.yml` runs the reconcile on relevant changes.

- **Triggers**: a push to `main` that changes `config.yaml`, the scripts, or
  the workflow itself; plus a manual **Run workflow** button
  (`workflow_dispatch`) with a **dry run** toggle.
- **Removal detection**: it checks out with `fetch-depth: 2` and extracts the
  previous `config.yaml` via `git show ${{ github.event.before }}:config.yaml`.
  The first push has no predecessor, so removals are skipped that one time.
- **Steps**: install deps → capture previous config → `setup_pais.py` (apply)
  → `cleanup_pais.py` (remove).
- **Safety**: a `concurrency` group serializes runs so two pushes can't
  reconcile the same instance simultaneously.

### 8.1 First push

From the `api-work` folder (which becomes the repository root):

```bash
cd C:\Users\nm019420\Demo-Tool\api-work
git init -b main               # only if not already a repo
git add .
git commit -m "Add PAIS GitOps tooling"
git remote add origin git@github-vcf.devops.broadcom.net:vcf/pais-gitops.git
git push -u origin main
```

After the push, create the secrets ([section 7](#7-creating-the-github-secrets))
and then either push a change to `config.yaml` or use **Actions ▸ PAIS GitOps ▸
Run workflow** to trigger the first reconcile.

### 8.2 Manual dry run from the UI

Go to **Actions ▸ PAIS GitOps ▸ Run workflow**, set **Dry run** to `true`, and
run. The logs show every create/update/delete that *would* happen, with no API
calls made.

---

## 9. End-to-end example

This walkthrough shows a typical change: starting from one knowledge base and
agent, then adding a second data source/KB and a new MCP tool, then removing
the original MCP server.

### Step 1 — Initial config

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
      embeddings_model_endpoint: "BAAI/bge-small-en-v1.5"
      text_splitting: "SENTENCE"
      chunk_size: 100
      chunk_overlap: 0
      trigger_indexing: true
      wait_for_indexing: true

mcp_servers:
  - name: "weather-service"
    url: "https://weather-mcp-server.example.com"
    transport: "STREAMABLE_HTTP"
    approve_tools: [ "get_current_weather" ]

agents:
  - name: "product-support-agent"
    model: "meta-llama/Meta-Llama-3.1-8B-Instruct"
    instructions: "You are a helpful product-support assistant."
    knowledge_bases:
      - { name: "product-docs-kb", top_n: 5, similarity_cutoff: 0.65 }
    mcp_tools:
      - { server: "weather-service", tool_name: "get_current_weather" }
```

Commit and push. The apply run will:

1. Test connectivity and create `gdrive-product-docs`.
2. Create `product-docs-kb`, link the data source, create
   `product-docs-index`, trigger indexing, and wait for `DONE`.
3. Register `weather-service` and approve `get_current_weather`.
4. Discover the REX tool `search_<index-hex>`.
5. Create `product-support-agent` wired to the REX tool and the weather tool.

Expected log tail:

```
=== Apply Complete ===
Data Sources   : 1
Knowledge Bases: 1
MCP Servers    : 1
MCP Tools      : 1 approved
Agents         : 1
  -> 'product-support-agent'  id=e52e4764-...  status=AVAILABLE
```

### Step 2 — Add a data source, KB, and a new tool

Edit `config.yaml`:

```yaml
data_sources:
  - name: "gdrive-product-docs"          # unchanged
    type: "GOOGLE_DRIVE"
    origin_url: "https://drive.google.com/drive/u/0/folders/abc123"
    credentials: "${GDRIVE_CREDENTIALS}"
    test_connection: true
  - name: "s3-support-articles"          # NEW
    type: "S3"
    origin_url: "s3://my-bucket/support-articles/"
    credentials: "${S3_CREDENTIALS}"
    test_connection: true

knowledge_bases:
  - name: "product-docs-kb"              # unchanged
    # ... (as before) ...
  - name: "support-articles-kb"          # NEW
    data_origin_type: "DATA_SOURCES"
    index_refresh_policy: { policy_type: "MANUAL" }
    data_sources: [ "s3-support-articles" ]
    index:
      name: "support-articles-index"
      embeddings_model_endpoint: "BAAI/bge-small-en-v1.5"
      text_splitting: "SENTENCE"
      chunk_size: 150
      chunk_overlap: 20
      trigger_indexing: true
      wait_for_indexing: true

mcp_servers:
  - name: "weather-service"
    url: "https://weather-mcp-server.example.com"
    transport: "STREAMABLE_HTTP"
    approve_tools: [ "get_current_weather", "get_forecast" ]   # ADDED get_forecast

agents:
  - name: "product-support-agent"
    model: "meta-llama/Meta-Llama-3.1-8B-Instruct"
    instructions: "You are a helpful product-support assistant."
    knowledge_bases:
      - { name: "product-docs-kb", top_n: 5, similarity_cutoff: 0.65 }
      - { name: "support-articles-kb", top_n: 3, similarity_cutoff: 0.70 }  # ADDED
    mcp_tools:
      - { server: "weather-service", tool_name: "get_current_weather" }
      - { server: "weather-service", tool_name: "get_forecast" }           # ADDED
```

Push. Because apply is **idempotent**, the unchanged objects are skipped; only
the new data source, KB/index, the newly approved `get_forecast`, and the
agent's updated tool list are applied. (Add `S3_CREDENTIALS` as a secret first.)

### Step 3 — Remove the MCP server

Delete the entire `weather-service` entry from `mcp_servers` **and** remove its
two `mcp_tools` from the agent, then push. The cleanup run compares the
previous commit to the new file and:

1. Deletes/updates affected agents first (so no tool is in use).
2. Deletes the `weather-service` MCP server (which removes its tools).

Expected cleanup log:

```
=== Removing MCP Servers: {'weather-service'} ===
  Deleted MCP server 'weather-service' (id=a1b2c3d4-...)
=== Cleanup Complete ===
MCP servers deleted   : 1
```

> If you remove a server but forget to remove its tools from an agent, the
> server delete can fail with **HTTP 409** (tool still linked). Remove the tool
> references from the agent in the same commit.

---

## 10. How additions and removals are reconciled

### Additions & updates (`setup_pais.py`)

- Every object is matched **by name**. If it exists, it is left as-is (data
  sources, KBs, indexes, MCP servers) or updated in place (agents).
- Data-source links and tool approvals are checked before acting, so re-runs
  are no-ops.
- Indexing is triggered only when an index is **first** created.

### Removals (`cleanup_pais.py`)

The script diffs the **previous** `config.yaml` (from the prior git commit)
against the **current** one and acts only on what disappeared:

| Change in config | Action |
| --- | --- |
| An `agents[]` entry removed | Delete the agent |
| A `knowledge_bases[]` entry removed | Delete the KB (cascades index + REX tool) |
| An `mcp_servers[]` entry removed | Delete the MCP server (cascades its tools) |
| A `data_sources[]` entry removed | Delete the data source |
| A name dropped from a server's `approve_tools` (server kept) | Un-approve that tool |
| A data source dropped from a kept KB's `data_sources` | Unlink it from the KB |

Deletions run in dependency-safe order: **agents → revoke tool approvals →
unlink KB data sources → knowledge bases → MCP servers → data sources.**

> **The key is `name`.** Renaming an item is treated as *remove old + add new*.
> Keep names unique and stable to avoid surprises.

---

## 11. Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `Missing required auth settings` | A `PAIS_*` secret/env var is unset. Check `gh secret list` and the workflow `env:` block. |
| `Environment variable ${X} is not set; leaving placeholder` | The config references `${X}` but no secret/env var named `X` exists. Add it. |
| Connectivity test fails for a data source | Wrong `origin_url`/`credentials`, or PAIS can't reach the source. Verify the JSON string and network egress. |
| REX tool "not found within Ns" | Index just created; tool registration lags. Increase `pais.rex_discovery_timeout_seconds`. |
| `409` deleting an MCP server / KB | A tool/index is still linked to an agent. Remove the agent's reference in the same commit. |
| `404` creating/updating an agent | A referenced tool id doesn't exist (e.g. approval was skipped). Check earlier log warnings. |
| Removals didn't run | First push (no previous commit), or the change wasn't to a watched path. Push a `config.yaml` change. |
| Duplicate objects appear | Two items share a `name`, or names were changed. Keep names unique/stable. |
| TLS errors against a dev instance | Set `PAIS_VERIFY_SSL=false` (dev only) or `pais.auth.verify_ssl: false`. |

Run any command with `--verbose` for DEBUG logging, and `--dry-run` to preview.

---

## 12. API endpoint reference

Endpoints used by the tooling (all under your PAIS `base_url`):

| Operation | Method & path |
| --- | --- |
| Test data-source connectivity | `POST /api/v1/control/data-sources/test-connection` |
| List / create data sources | `GET` / `POST /api/v1/control/data-sources` |
| Delete data source | `DELETE /api/v1/control/data-sources/{id}` |
| List / create knowledge bases | `GET` / `POST /api/v1/control/knowledge-bases` |
| Delete knowledge base | `DELETE /api/v1/control/knowledge-bases/{id}` |
| List / create KB data-source links | `GET` / `POST /api/v1/control/knowledge-bases/{kb_id}/data-sources` |
| Delete KB data-source link | `DELETE /api/v1/control/knowledge-bases/{kb_id}/data-sources/{link_id}` |
| List / create indexes | `GET` / `POST /api/v1/control/knowledge-bases/{kb_id}/indexes` |
| Trigger indexing | `POST /api/v1/control/knowledge-bases/{kb_id}/indexes/{index_id}/indexings` |
| Poll active indexing | `GET /api/v1/control/knowledge-bases/{kb_id}/indexes/{index_id}/active-indexing` |
| List / create MCP servers | `GET` / `POST /api/v1/control/mcp-servers` |
| Delete MCP server | `DELETE /api/v1/control/mcp-servers/{id}` |
| List MCP tools | `GET /api/v1/control/mcp-servers/tools?server={id|built-in}` |
| Approve / un-approve tool | `POST /api/v1/control/mcp-servers/{server_id}/tools/{tool_id}/approval` |
| List / create agents | `GET` / `POST /api/v1/compatibility/openai/v1/agents` |
| Update / delete agent | `POST` / `DELETE /api/v1/compatibility/openai/v1/agents/{id}` |

Built-in REX search tools are auto-created per index and named
`search_<index-id-hex>` (the index UUID with hyphens removed).
