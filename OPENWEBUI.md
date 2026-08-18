# OpenWebUI Integration with VCF Private AI Services (PAIS)

This guide details how to automatically connect an **existing OpenWebUI installation** to VMware Cloud Foundation (VCF) Private AI Services (PAIS) using PAIS's OpenAI-compatible API endpoints and Authentik JWT Bearer tokens.

---

## 1. Declarative Configuration in `config.yaml`

To enable OpenWebUI auto-configuration, add the `openwebui:` block to your tenant's `config.yaml` file (e.g. `tenants/pais-all-apps/config.yaml`):

```yaml
# ============================================================
# 7. OPENWEBUI INTEGRATION
# ============================================================
openwebui:
  enabled: true                           # Set to true to automatically configure PAIS endpoint in OpenWebUI
  url: "${OPENWEBUI_URL}"                 # Base URL of existing OpenWebUI instance, e.g. http://10.138.218.100:3000
  api_key: "${OPENWEBUI_API_KEY}"         # OpenWebUI Admin API key or JWT Bearer token
  username: "${OPENWEBUI_USERNAME}"       # Fallback if api_key not provided
  password: "${OPENWEBUI_PASSWORD}"       # Fallback if api_key not provided
  verify_ssl: false                       # Verify SSL certificate for OpenWebUI URL
  openai_base_url: "https://10.138.217.12/api/v1/compatibility/openai/v1" # PAIS OpenAI compatibility endpoint
  default_model: "gpt-oss-20b-shared"     # Primary completion model to set as active/default in OpenWebUI
```

---

## 2. Automated GitOps Execution & GitHub Actions

When you run `setup_pais.py`, `cleanup_pais.py`, or the GitHub Actions pipeline (`pais-gitops.yml`), OpenWebUI connections and models are automatically managed:

```bash
# Reconcile PAIS resources & auto-configure OpenWebUI:
python setup_pais.py --config tenants/pais-all-apps/config.yaml
```

### GitHub Actions Integration
Secrets for OpenWebUI can be stored in GitHub Repository Secrets:
* `OPENWEBUI_URL` (e.g. `http://10.138.218.100:3000`)
* `OPENWEBUI_API_KEY` (or `OPENWEBUI_USERNAME` / `OPENWEBUI_PASSWORD`)

The workflow (`.github/workflows/pais-gitops.yml`) injects these secrets and executes `reconcile_all.py` on push to `main`.

---

## 3. Removing Connections & Models from OpenWebUI

If you wish to remove the PAIS connection and its associated models from OpenWebUI, update `config.yaml`:

```yaml
openwebui:
  enabled: false                          # Set to false to remove PAIS connection & models from OpenWebUI
  remove: true                            # Or set remove: true / state: "absent"
  url: "${OPENWEBUI_URL}"
  api_key: "${OPENWEBUI_API_KEY}"
```

When `setup_pais.py` or `cleanup_pais.py` runs with `enabled: false` or `remove: true`:
1. It connects to the OpenWebUI REST API (`GET /api/v1/configs/openai`).
2. It locates and deletes the tenant's PAIS OpenAI Base URL (`https://10.138.217.12/api/v1/compatibility/openai/v1`) from `OPENAI_API_BASE_URLS` and its key from `OPENAI_API_KEYS`.
3. OpenWebUI immediately purges all models associated with that PAIS endpoint from its active chat model dropdown.

---

## 3. Standalone Execution

You can also run the OpenWebUI integration script standalone at any time:

```bash
python setup_openwebui.py --config tenants/pais-all-apps/config.yaml
```

---

## 4. Manual Connection Configuration (OpenWebUI Web Interface)

If you prefer to configure OpenWebUI via the browser UI:

1. Log in to **OpenWebUI** -> Go to **Admin Panel** -> **Settings** -> **Connections**.
2. Under **OpenAI API Connections**:
   * **URL / Base API**: `https://10.138.217.12/api/v1/compatibility/openai/v1`
   * **API Key**: `<YOUR_AUTHENTIK_JWT_BEARER_TOKEN>`
3. Click **Verify Connection** (refresh icon). OpenWebUI will query PAIS `/v1/models` and discover all PAIS models and agents.
4. Save the configuration.
5. Select **`gpt-oss-20b-shared`** from the model dropdown and start chatting!

---

## 5. Endpoints & Architecture Summary

| Component | Endpoint |
|---|---|
| **PAIS OpenAI Compatibility API** | `https://10.138.217.12/api/v1/compatibility/openai/v1` |
| **Models Discovery** | `GET https://10.138.217.12/api/v1/compatibility/openai/v1/models` |
| **Chat Completion** | `POST https://10.138.217.12/api/v1/compatibility/openai/v1/chat/completions` |
| **Authentik OIDC Token URL** | `https://auth01.vcf05.showcase.tmm.broadcom.lab/application/o/token/` |
| **OpenWebUI API Config Endpoint** | `POST http://<openwebui-host>:3000/api/v1/configs/openai` |

---

## 6. Verifying Chat Completion via Command Line

You can test the PAIS chat endpoint directly with `curl`:

```bash
curl -k -X POST "https://10.138.217.12/api/v1/compatibility/openai/v1/chat/completions" \
  -H "Authorization: Bearer $JWT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-oss-20b-shared",
    "messages": [{"role": "user", "content": "What is VCF Private AI Services?"}],
    "temperature": 0.7
  }'
```
