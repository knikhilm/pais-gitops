# OpenWebUI Integration with VCF Private AI Services (PAIS)

This guide details how to integrate **OpenWebUI** with VMware Cloud Foundation (VCF) Private AI Services (PAIS) using PAIS's OpenAI-compatible API endpoints and Authentik JWT Bearer tokens.

---

## 1. Quick Start (Automated Setup)

Run `setup_openwebui.py` to automatically authenticate with Authentik, acquire a JWT Bearer token, verify PAIS completion models, and generate ready-to-use OpenWebUI configuration files:

```bash
python setup_openwebui.py --config tenants/pais-all-apps/config.yaml
```

### What `setup_openwebui.py` Does:
1. **Acquires Bearer JWT Token**: Authenticates against Authentik OIDC using credentials in `config.yaml`.
2. **Discovers Models**: Verifies `GET https://10.138.217.12/api/v1/compatibility/openai/v1/models` and lists available completion models (e.g. `gpt-oss-20b-shared`, `gpt-oss-20b`).
3. **Tests Chat Completion**: Sends a test completion request to PAIS to ensure the model is initialized and ready to chat.
4. **Generates Docker Configs**: Creates `.env.openwebui` and `docker-compose.openwebui.yml` pre-populated with PAIS endpoints and the JWT token.

---

## 2. Launching OpenWebUI

Launch OpenWebUI using Docker Compose:

```bash
docker compose -f docker-compose.openwebui.yml up -d
```

Open your browser to [http://localhost:3000](http://localhost:3000). OpenWebUI will start with PAIS pre-configured as its primary LLM provider and default model `gpt-oss-20b-shared` selected for chat!

---

## 3. Manual Connection Configuration (OpenWebUI Web Interface)

If you already have OpenWebUI running, you can connect it to PAIS via the web interface:

1. Open **OpenWebUI** -> Go to **Admin Panel** -> **Settings** -> **Connections**.
2. Under **OpenAI API Connections**:
   * **URL / Base API**: `https://10.138.217.12/api/v1/compatibility/openai/v1`
   * **API Key**: `<YOUR_AUTHENTIK_JWT_BEARER_TOKEN>`
3. Click **Verify Connection** (the refresh icon). OpenWebUI will query PAIS `/v1/models` and load all available PAIS models and agents.
4. Save the configuration.
5. In the chat interface, select **`gpt-oss-20b-shared`** (or your PAIS model/agent) from the top dropdown menu and start chatting!

---

## 4. How PAIS Authentication Works with OpenWebUI

PAIS OpenAI-compatible endpoints require a **Bearer JWT Token** in the HTTP request header:

```http
Authorization: Bearer eyJhbGciOiJSUzI1NiIs...
```

### Fetching a Bearer JWT Token via `curl`

To fetch a Bearer JWT token directly using `curl`:

```bash
JWT_TOKEN=$(curl -k -s -X POST "https://auth01.vcf05.showcase.tmm.broadcom.lab/application/o/token/" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=password" \
  -d "client_id=ipSk5yCnmjRUMjSW3YQ7ikPUhJDU82pi0uz9mC2i" \
  -d "username=pais-user" \
  -d "password=YOUR_APP_PASSWORD" \
  -d "scope=openid groups offline_access" | jq -r '.access_token')

echo "JWT Token: $JWT_TOKEN"
```

> **Note**: Standard OIDC user passwords may require an **App Password** generated under **Authentik -> User Profile Settings -> App Passwords** or a permanent API token (`expiring: false`).

---

## 5. Endpoints & Architecture Summary

| Component | Endpoint |
|---|---|
| **PAIS OpenAI Compatibility API** | `https://10.138.217.12/api/v1/compatibility/openai/v1` |
| **Models Discovery** | `GET https://10.138.217.12/api/v1/compatibility/openai/v1/models` |
| **Chat Completion** | `POST https://10.138.217.12/api/v1/compatibility/openai/v1/chat/completions` |
| **Authentik OIDC Token URL** | `https://auth01.vcf05.showcase.tmm.broadcom.lab/application/o/token/` |
| **OpenWebUI Local URL** | `http://localhost:3000` |

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
