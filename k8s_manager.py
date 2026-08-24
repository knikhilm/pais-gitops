"""
Kubernetes Custom Resource & Authentication Manager for VMware Private AI Services (PAIS).

Handles:
  1. Cluster Authentication & Login:
     - vSphere with Tanzu SSO Login (`kubectl vsphere login`) using vSphere credentials.
     - Direct ServiceAccount / Bearer Token & Server URL login (`kubectl config set-credentials`).
     - Kubeconfig environment interpolation (`KUBECONFIG` / `KUBECONFIG_DATA`).
  2. Image & Registry Secret Provisioning:
     - Generates `kubernetes.io/dockerconfigjson` Secrets for OCI registries (e.g., Harbor)
       so VKS node pools can pull model artifacts specified in ModelEndpoints.
     - Generates `pais.vmware.com/api-token-credentials` Secrets for InferenceGatewayRoute backends.
  3. Custom Resource Manifest Generation & Reconcile:
     - ModelEndpoint         (apiGroup: pais.vmware.com/v1alpha1)
     - InferenceGatewayRoute  (apiGroup: pais.vmware.com/v1alpha1)
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
import time
from typing import Any

import yaml

log = logging.getLogger("pais.k8s")


# ---------------------------------------------------------------------------
# 1. Cluster Authentication & Login Helpers
# ---------------------------------------------------------------------------

def authenticate_k8s_cluster(config: dict, dry_run: bool = False) -> bool:
    """
    Attempt to log into the target Kubernetes cluster (vSphere Supervisor/VKS or standard K8s).

    Tries in order:
      1. VCF CLI login (`vcf context create` and `vcf context use`) if VCF/vSphere credentials exist.
      2. ServiceAccount / Bearer token login if KUBE_SERVER and KUBE_TOKEN exist.
      3. Existing active kubeconfig context.
    """
    if dry_run:
        log.info("  [dry-run] K8s authentication check completed (skipped actual login).")
        return True

    pais_cfg = config.get("pais", {})
    k8s_cfg = config.get("kubernetes", pais_cfg)
    vsphere_cfg = k8s_cfg.get("vsphere", {})

    def _cfg_or_env(cfg_val: str, env_vars: list[str], default: str = "") -> str:
        s_val = str(cfg_val or "").strip()
        if s_val and not s_val.startswith("${"):
            return s_val
        for ev in env_vars:
            ev_val = os.environ.get(ev, "").strip()
            if ev_val:
                return ev_val
        return default

    # Method 1: VCF CLI Authentication (vcf context create & vcf context use)
    v_server = _cfg_or_env(vsphere_cfg.get("server"), ["VCF_ENDPOINT", "VSPHERE_SERVER"])
    v_token = _cfg_or_env(vsphere_cfg.get("api_token"), ["VCF_API_TOKEN"])
    v_user = _cfg_or_env(vsphere_cfg.get("username"), ["VCF_USER", "VSPHERE_USER"])
    v_pass = _cfg_or_env(vsphere_cfg.get("password"), ["VCF_PASSWORD", "VSPHERE_PASSWORD"])
    v_type = _cfg_or_env(vsphere_cfg.get("type"), ["VCF_TYPE"], "cci")
    v_auth_type = _cfg_or_env(vsphere_cfg.get("auth_type"), ["VCF_AUTH_TYPE"], "basic")
    v_tenant = _cfg_or_env(vsphere_cfg.get("tenant_name"), ["VCF_TENANT_NAME"], "all-apps")
    v_ns = _cfg_or_env(vsphere_cfg.get("namespace", k8s_cfg.get("namespace")), ["VCF_NAMESPACE", "VSPHERE_NAMESPACE"])
    v_project = _cfg_or_env(vsphere_cfg.get("project_name", vsphere_cfg.get("project")), ["VCF_PROJECT", "PROJECT_NAME"])
    v_ctx_name = _cfg_or_env(vsphere_cfg.get("context_name"), ["VCF_CONTEXT_NAME"], "vcf05paif")

    if v_server and (v_token or (v_user and v_pass)):
        # 1. Clean up existing context if present to ensure clean create
        log.info("Cleaning up existing VCF context '%s' if present...", v_ctx_name)
        try:
            subprocess.run(["vcf", "context", "delete", v_ctx_name, "--yes"], capture_output=True, text=True, timeout=30)
        except Exception:
            pass

        # 2. Create VCF context
        log.info("Authenticating to VCF Cluster via 'vcf context create'...")
        cmd_create = [
            "vcf", "context", "create", v_ctx_name,
            "--endpoint", v_server,
            "--type", v_type,
            "--auth-type", v_auth_type,
            "--insecure-skip-tls-verify",
        ]
        if v_token:
            cmd_create.extend(["--api-token", v_token])
        else:
            cmd_create.extend(["--username", v_user, "--password", v_pass])

        if v_tenant:
            cmd_create.extend(["--tenant-name", v_tenant])

        try:
            res_create = subprocess.run(cmd_create, capture_output=True, text=True, timeout=60)
            if res_create.returncode == 0:
                log.info("VCF context '%s' created successfully: %s", v_ctx_name, res_create.stdout.strip())
            else:
                log.warning(
                    "VCF context create warning/error (code %d):\nSTDOUT: %s\nSTDERR: %s",
                    res_create.returncode,
                    res_create.stdout.strip(),
                    res_create.stderr.strip(),
                )
        except Exception as exc:
            log.warning("VCF context create exception: %s", exc)

        # 3. Switch context
        if v_ns and v_project:
            full_context = f"{v_ctx_name}:{v_ns}:{v_project}"
        elif v_ns:
            full_context = f"{v_ctx_name}:{v_ns}"
        else:
            full_context = v_ctx_name

        cmd_use = ["vcf", "context", "use", full_context]
        log.info("Switching VCF context via 'vcf context use %s'...", full_context)

        token_input = f"{v_token}\n" if v_token else (f"{v_pass}\n" if v_pass else None)
        try:
            res_use = subprocess.run(cmd_use, input=token_input, capture_output=True, text=True, timeout=60)
            if res_use.returncode == 0:
                log.info("VCF context switched to '%s': %s", full_context, res_use.stdout.strip())
                return True
            else:
                log.warning(
                    "VCF context use failed (code %d):\nSTDOUT: %s\nSTDERR: %s. Falling back to default kubeconfig...",
                    res_use.returncode,
                    res_use.stdout.strip(),
                    res_use.stderr.strip(),
                )
        except Exception as exc:
            log.warning("VCF context use exception: %s", exc)

    # Method 2: ServiceAccount / Bearer Token Login
    k_server = os.environ.get("KUBE_SERVER", k8s_cfg.get("server", ""))
    k_token = os.environ.get("KUBE_TOKEN", k8s_cfg.get("token", ""))
    k_ns = os.environ.get("KUBE_NAMESPACE", k8s_cfg.get("namespace", "default"))

    if k_server and k_token:
        log.info("Authenticating to K8s cluster via ServiceAccount bearer token...")
        try:
            subprocess.run(["kubectl", "config", "set-cluster", "pais-cluster", f"--server={k_server}", "--insecure-skip-tls-verify=true"], check=True, capture_output=True)
            subprocess.run(["kubectl", "config", "set-credentials", "pais-sa", f"--token={k_token}"], check=True, capture_output=True)
            subprocess.run(["kubectl", "config", "set-context", "pais-context", "--cluster=pais-cluster", "--user=pais-sa", f"--namespace={k_ns}"], check=True, capture_output=True)
            subprocess.run(["kubectl", "config", "use-context", "pais-context"], check=True, capture_output=True)
            log.info("ServiceAccount K8s authentication context set successfully.")
            return True
        except Exception as exc:
            log.warning("ServiceAccount token login failed: %s. Falling back to default context...", exc)

    # Method 3: Existing active kubectl context
    if _is_kubectl_available():
        try:
            res = subprocess.run(["kubectl", "config", "current-context"], capture_output=True, text=True, check=True)
            log.info("Using active Kubernetes context: '%s'", res.stdout.strip())
            return True
        except Exception:
            pass

    log.warning("No active Kubernetes authentication configured. Manifests will be output to file.")
    return False


# ---------------------------------------------------------------------------
# 2. Secret Provisioning Builders (Image Pull & Backend API Tokens)
# ---------------------------------------------------------------------------

def build_docker_registry_secret(registry_cfg: dict, default_namespace: str | None = None) -> dict[str, Any] | None:
    """
    Generate a Kubernetes secret of type `kubernetes.io/dockerconfigjson`
    used by VKS node pools to pull OCI model artifacts from Harbor/registries.

    Accounts for self-signed certificates via:
      1. `insecure` / `insecure_skip_tls_verify`: Adds `pais.vmware.com/insecure-registry` annotations/labels.
      2. `ca_cert`: Injects `ca.crt` PEM certificate data into the secret.
    """
    server = os.environ.get("HARBOR_REGISTRY", os.environ.get("REGISTRY_SERVER", registry_cfg.get("server", "")))
    user = os.environ.get("HARBOR_USERNAME", os.environ.get("REGISTRY_USERNAME", registry_cfg.get("username", "")))
    password = os.environ.get("HARBOR_PASSWORD", os.environ.get("REGISTRY_PASSWORD", registry_cfg.get("password", "")))
    secret_name = registry_cfg.get("secret_name", "harbor-registry-secret")
    namespace = registry_cfg.get("namespace", default_namespace)

    # Self-signed certificate handling options
    insecure_env = os.environ.get("HARBOR_INSECURE", os.environ.get("REGISTRY_INSECURE", "")).lower() in ("true", "1", "yes")
    insecure_cfg = registry_cfg.get("insecure", registry_cfg.get("insecure_skip_tls_verify", False))
    is_insecure = insecure_env or bool(insecure_cfg)

    ca_cert = os.environ.get("HARBOR_CA_CERT", registry_cfg.get("ca_cert", ""))

    if not server or not user or not password:
        return None

    auth_str = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("utf-8")
    docker_config = {
        "auths": {
            server: {
                "username": user,
                "password": password,
                "auth": auth_str,
            }
        }
    }
    docker_json = json.dumps(docker_config)
    encoded_docker_json = base64.b64encode(docker_json.encode("utf-8")).decode("utf-8")

    labels: dict[str, str] = {
        "app.kubernetes.io/managed-by": "pais-gitops",
    }
    annotations: dict[str, str] = {}

    if is_insecure:
        labels["pais.vmware.com/insecure-registry"] = "true"
        annotations["pais.vmware.com/insecure-registry"] = "true"

    secret_data: dict[str, str] = {
        ".dockerconfigjson": encoded_docker_json,
    }

    if ca_cert:
        secret_data["ca.crt"] = base64.b64encode(ca_cert.strip().encode("utf-8")).decode("utf-8")

    metadata: dict[str, Any] = {
        "name": secret_name,
        "labels": labels,
    }
    if namespace:
        metadata["namespace"] = namespace
    if annotations:
        metadata["annotations"] = annotations

    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": metadata,
        "type": "kubernetes.io/dockerconfigjson",
        "data": secret_data,
    }


def build_api_token_secret(token_cfg: dict, default_namespace: str | None = None) -> dict[str, Any] | None:
    """
    Generate a Kubernetes secret of type `pais.vmware.com/api-token-credentials`
    used by InferenceGatewayRoute backend authentication.
    """
    name = token_cfg.get("name")
    api_token = os.environ.get(token_cfg.get("env_var", ""), token_cfg.get("api_token", ""))
    namespace = token_cfg.get("namespace", default_namespace)

    if not name or not api_token:
        return None

    encoded_token = base64.b64encode(api_token.encode("utf-8")).decode("utf-8")

    metadata: dict[str, Any] = {
        "name": name,
        "labels": {
            "app.kubernetes.io/managed-by": "pais-gitops",
        },
    }
    if namespace:
        metadata["namespace"] = namespace

    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": metadata,
        "type": "pais.vmware.com/api-token-credentials",
        "data": {
            "api_token": encoded_token,
        },
    }


# ---------------------------------------------------------------------------
# 3. CRD Manifest Builders
# ---------------------------------------------------------------------------

def build_model_endpoint_crd(endpoint_cfg: dict, default_namespace: str | None = None) -> dict[str, Any]:
    """
    Construct a Kubernetes Custom Resource object for a PAIS ModelEndpoint.
    """
    name = endpoint_cfg["name"]
    namespace = endpoint_cfg.get("namespace", default_namespace)

    spec: dict[str, Any] = {
        "type": endpoint_cfg.get("type", "Completions"),
        "engine": endpoint_cfg.get("engine", "vLLM"),
        "routingName": endpoint_cfg["routing_name"],
        "replicas": endpoint_cfg.get("replicas", 1),
        "overrides": endpoint_cfg.get("overrides", ""),
    }

    if "virtual_machine_class_name" in endpoint_cfg:
        spec["virtualMachineClassName"] = endpoint_cfg["virtual_machine_class_name"]

    if "storage_class_name" in endpoint_cfg:
        spec["storageClassName"] = endpoint_cfg["storage_class_name"]

    if "failure_domain" in endpoint_cfg:
        spec["failureDomain"] = endpoint_cfg["failure_domain"]

    # Model OCI reference & pull secrets
    model_cfg = endpoint_cfg.get("model", {})
    if model_cfg:
        model_spec: dict[str, Any] = {}
        if "oci_ref" in model_cfg:
            model_spec["ociRef"] = model_cfg["oci_ref"]
        if "pull_secrets" in model_cfg:
            model_spec["pullSecrets"] = model_cfg["pull_secrets"]
        spec["model"] = model_spec

    # Customization (CLI args, Env vars, engine image, sizes)
    cust = endpoint_cfg.get("inference_server_customization")
    if cust:
        cust_spec: dict[str, Any] = {}
        if "cli_args" in cust:
            cust_spec["cliArgs"] = cust["cli_args"]
        if cust.get("env_vars"):
            cust_spec["envVars"] = cust["env_vars"]
        if "engine_image" in cust:
            cust_spec["engineImage"] = cust["engine_image"]
        if "engine_image_compressed_size" in cust:
            cust_spec["engineImageCompressedSize"] = cust["engine_image_compressed_size"]
        if "shared_memory_mount_size" in cust:
            cust_spec["sharedMemoryMountSize"] = cust["shared_memory_mount_size"]
        if "temp_mount_size" in cust:
            cust_spec["tempMountSize"] = cust["temp_mount_size"]
        spec["inferenceServerCustomization"] = cust_spec

    annotations = dict(endpoint_cfg.get("annotations", {}))
    annotations["pais.vmware.com/displayName"] = endpoint_cfg.get("display_name", name)

    metadata: dict[str, Any] = {
        "name": name,
        "annotations": annotations,
        "labels": {
            "app.kubernetes.io/managed-by": "pais-gitops",
            "pais.vmware.com/routing-name": endpoint_cfg["routing_name"].replace("/", "-").lower(),
        },
    }
    if namespace:
        metadata["namespace"] = namespace

    return {
        "apiVersion": "pais.vmware.com/v1alpha1",
        "kind": "ModelEndpoint",
        "metadata": metadata,
        "spec": spec,
    }


def build_inference_gateway_route_crd(route_cfg: dict, default_namespace: str | None = None) -> dict[str, Any]:
    """
    Construct a Kubernetes Custom Resource object for a PAIS InferenceGatewayRoute.
    """
    name = route_cfg["name"]
    namespace = route_cfg.get("namespace", default_namespace)

    spec: dict[str, Any] = {
        "type": route_cfg.get("type", "Completions"),
        "engine": route_cfg.get("engine", "vLLM"),
        "matches": {
            "routingName": route_cfg.get("matches", {}).get("routing_name", name),
        },
    }

    backend_cfg = route_cfg.get("backend", {})
    if backend_cfg:
        backend_spec: dict[str, Any] = {}
        if "http_base_url" in backend_cfg:
            backend_spec["httpBaseUrl"] = backend_cfg["http_base_url"]
        if "model_id" in backend_cfg:
            backend_spec["modelId"] = backend_cfg["model_id"]
        if "tls" in backend_cfg:
            backend_spec["tls"] = backend_cfg["tls"]
        if "auth" in backend_cfg:
            auth_cfg = backend_cfg["auth"]
            auth_spec = {}
            if isinstance(auth_cfg, dict):
                if "api_token_ref" in auth_cfg:
                    auth_spec["apiTokenRef"] = auth_cfg["api_token_ref"]
                elif "apiTokenRef" in auth_cfg:
                    auth_spec["apiTokenRef"] = auth_cfg["apiTokenRef"]
                else:
                    auth_spec = auth_cfg
            else:
                auth_spec = auth_cfg
            backend_spec["auth"] = auth_spec
        if backend_spec:
            spec["backend"] = backend_spec

    metadata: dict[str, Any] = {
        "name": name,
        "labels": {
            "app.kubernetes.io/managed-by": "pais-gitops",
        },
    }
    if namespace:
        metadata["namespace"] = namespace

    return {
        "apiVersion": "pais.vmware.com/v1alpha1",
        "kind": "InferenceGatewayRoute",
        "metadata": metadata,
        "spec": spec,
    }


# ---------------------------------------------------------------------------
# 4. Manifest Generation & File Export
# ---------------------------------------------------------------------------

def generate_k8s_manifests(config: dict) -> list[dict[str, Any]]:
    """
    Build all Kubernetes manifests (Secrets, ModelEndpoints, InferenceGatewayRoutes).
    """
    pais_cfg = config.get("pais", {})
    k8s_cfg = config.get("kubernetes", pais_cfg)
    default_ns = k8s_cfg.get("namespace")  # Omit namespace if not explicitly configured

    manifests: list[dict[str, Any]] = []

    # 1. Registry Secrets (dockerconfigjson)
    reg_secret = build_docker_registry_secret(k8s_cfg.get("registry", {}), default_namespace=default_ns)
    if reg_secret:
        manifests.append(reg_secret)

    # 2. API Token Secrets (pais.vmware.com/api-token-credentials)
    for token_cfg in k8s_cfg.get("api_tokens", []):
        tok_secret = build_api_token_secret(token_cfg, default_namespace=default_ns)
        if tok_secret:
            manifests.append(tok_secret)

    # 3. ModelEndpoints
    for me in config.get("model_endpoints", []):
        manifests.append(build_model_endpoint_crd(me, default_namespace=default_ns))

    # 4. InferenceGatewayRoutes
    for ir in config.get("inference_routes", []):
        manifests.append(build_inference_gateway_route_crd(ir, default_namespace=default_ns))

    return manifests


def write_manifests_file(manifests: list[dict[str, Any]], output_path: str) -> None:
    """
    Write manifests list to a multi-document YAML file.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        yaml.dump_all(manifests, fh, default_flow_style=False, sort_keys=False)
    log.info("Wrote %d Kubernetes manifests to '%s'", len(manifests), output_path)


# ---------------------------------------------------------------------------
# 5. Kubectl Execution
# ---------------------------------------------------------------------------

def apply_k8s_manifests(config: dict, manifests: list[dict[str, Any]], dry_run: bool = False) -> None:
    """
    Authenticate and apply Kubernetes manifests via `kubectl apply`.
    """
    if not manifests:
        log.info("No Kubernetes ModelEndpoint or InferenceGatewayRoute resources defined.")
        return

    yaml_content = yaml.dump_all(manifests, default_flow_style=False, sort_keys=False)

    if dry_run:
        log.info("=== Kubernetes Manifests [DRY-RUN] ===")
        for line in yaml_content.splitlines():
            log.info("  %s", line)
        return

    # Cluster Authentication
    authenticated = authenticate_k8s_cluster(config, dry_run=dry_run)
    if not authenticated or not _is_kubectl_available():
        log.warning(
            "kubectl is not authenticated or not available. Skipping direct cluster apply. "
            "Manifests have been saved to disk in k8s-manifests/ for GitOps/manual deployment."
        )
        return

    log.info("Applying %d Kubernetes manifests via kubectl apply...", len(manifests))
    try:
        res = subprocess.run(
            ["kubectl", "apply", "--validate=false", "-f", "-"],
            input=yaml_content,
            text=True,
            capture_output=True,
            check=True,
        )
        log.info("Kubectl apply output:\n%s", res.stdout.strip())
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"kubectl apply failed: {exc.stderr.strip()}") from exc


def wait_for_model_endpoints(
    config: dict,
    dry_run: bool = False,
    timeout_seconds: int = 3600,
    poll_interval_seconds: int = 30,
) -> None:
    """
    Poll applied ModelEndpoints until they reach Ready/Running/Deployed status
    before proceeding to RAG/MCP/Agents. Models can take 30-40 minutes to deploy.
    """
    endpoints = config.get("model_endpoints", [])
    if not endpoints:
        return

    if dry_run:
        log.info("  [dry-run] Skipping wait for ModelEndpoints.")
        return

    if not _is_kubectl_available():
        log.warning("kubectl not available; skipping ModelEndpoint status wait.")
        return

    pending_names = [me["name"] for me in endpoints if isinstance(me, dict) and me.get("name")]
    if not pending_names:
        return

    log.info("")
    log.info("==========================================================================")
    log.info("Waiting for ModelEndpoints to deploy (%s) - Timeout: %d mins", ", ".join(pending_names), timeout_seconds // 60)
    log.info("Models typically take 30 to 40 minutes to fully download and initialize.")
    log.info("==========================================================================")

    start_time = time.time()

    while pending_names and (time.time() - start_time) < timeout_seconds:
        still_pending = []
        for name in pending_names:
            try:
                res = subprocess.run(
                    ["kubectl", "get", "modelendpoint", name, "-o", "json"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if res.returncode != 0:
                    still_pending.append(name)
                    continue

                obj = json.loads(res.stdout)
                status = obj.get("status", {})
                phase = str(status.get("phase", "")).lower()

                conditions = status.get("conditions", [])
                ready_condition = any(
                    c.get("type") in ("Ready", "Available") and str(c.get("status")).lower() == "true"
                    for c in conditions if isinstance(c, dict)
                )

                if phase in ("ready", "running", "deployed") or ready_condition:
                    log.info("ModelEndpoint '%s' is READY! (phase='%s')", name, status.get("phase", "Ready"))
                else:
                    elapsed = int(time.time() - start_time)
                    log.info("  ModelEndpoint '%s' deploying... status phase='%s' (%d mins %ds elapsed)",
                             name, status.get("phase", "Pending"), elapsed // 60, elapsed % 60)
                    still_pending.append(name)
            except Exception as exc:
                log.debug("Error checking status for ModelEndpoint '%s': %s", name, exc)
                still_pending.append(name)

        pending_names = still_pending
        if pending_names:
            time.sleep(poll_interval_seconds)

    if pending_names:
        log.warning("Timed out waiting for ModelEndpoints: %s. Proceeding with remaining steps.", pending_names)
    else:
        log.info("All ModelEndpoints are READY! Proceeding to Data Sources, Knowledge Bases, MCP Servers, and Agents.")


def delete_removed_k8s_resources(old_config: dict, new_config: dict, dry_run: bool = False) -> None:
    """
    Identify deleted ModelEndpoints and InferenceGatewayRoutes and remove them via kubectl.
    """
    if not isinstance(old_config, dict):
        old_config = {}
    if not isinstance(new_config, dict):
        new_config = {}

    old_endpoints = {me["name"]: me for me in old_config.get("model_endpoints", []) or [] if isinstance(me, dict) and me.get("name")}
    new_endpoints = {me["name"]: me for me in new_config.get("model_endpoints", []) or [] if isinstance(me, dict) and me.get("name")}
    removed_endpoints = set(old_endpoints) - set(new_endpoints)

    old_routes = {ir["name"]: ir for ir in old_config.get("inference_routes", []) or [] if isinstance(ir, dict) and ir.get("name")}
    new_routes = {ir["name"]: ir for ir in new_config.get("inference_routes", []) or [] if isinstance(ir, dict) and ir.get("name")}
    removed_routes = set(old_routes) - set(new_routes)

    if not removed_endpoints and not removed_routes:
        log.info("No Kubernetes ModelEndpoints or InferenceGatewayRoutes removed.")
        return

    # Authenticate before deletion
    if not dry_run:
        authenticate_k8s_cluster(new_config, dry_run=dry_run)

    k8s_val = new_config.get("kubernetes")
    if not isinstance(k8s_val, dict):
        k8s_val = new_config.get("pais") if isinstance(new_config.get("pais"), dict) else {}
    default_ns = k8s_val.get("namespace") if isinstance(k8s_val, dict) else None

    for name in sorted(removed_routes):
        route = old_endpoints.get(name) or old_routes.get(name) or {}
        ns = route.get("namespace", default_ns)
        log.info("Removing InferenceGatewayRoute '%s'...", name)
        cmd = ["delete", "inferencegatewayroute", name, "--ignore-not-found=true"]
        if ns:
            cmd.extend(["-n", ns])
        if dry_run:
            log.info("  [dry-run] kubectl %s", " ".join(cmd))
        elif _is_kubectl_available():
            _run_kubectl(cmd)

    for name in sorted(removed_endpoints):
        endpoint = old_endpoints.get(name) or {}
        ns = endpoint.get("namespace", default_ns)
        log.info("Removing ModelEndpoint '%s'...", name)
        cmd = ["delete", "modelendpoint", name, "--ignore-not-found=true"]
        if ns:
            cmd.extend(["-n", ns])
        if dry_run:
            log.info("  [dry-run] kubectl %s", " ".join(cmd))
        elif _is_kubectl_available():
            _run_kubectl(cmd)


def _is_kubectl_available() -> bool:
    try:
        subprocess.run(["kubectl", "version", "--client"], capture_output=True, check=True)
        return True
    except Exception:
        return False


def _run_kubectl(args: list[str]) -> str:
    cmd = ["kubectl"] + args
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 and "not found" not in res.stderr.lower():
        log.warning("kubectl command '%s' warnings/errors: %s", " ".join(cmd), res.stderr.strip())
    else:
        log.info("kubectl output: %s", res.stdout.strip())
    return res.stdout.strip()
