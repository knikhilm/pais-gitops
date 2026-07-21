"""
Kubernetes Custom Resource Manager for VMware Private AI Services (PAIS).

Handles the generation, validation, application, and deletion of Kubernetes CRDs:
  * ModelEndpoint         (apiGroup: pais.vmware.com/v1alpha1)
  * InferenceGatewayRoute  (apiGroup: pais.vmware.com/v1alpha1)

Can run via ``kubectl`` CLI if available, or generate standalone Kubernetes
manifest files suitable for GitOps tools like ArgoCD / Flux or manual kubectl apply.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from typing import Any

import yaml

log = logging.getLogger("pais.k8s")


# ---------------------------------------------------------------------------
# CRD Manifest Builders
# ---------------------------------------------------------------------------

def build_model_endpoint_crd(endpoint_cfg: dict, default_namespace: str = "default") -> dict[str, Any]:
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
        if "env_vars" in cust:
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

    return {
        "apiVersion": "pais.vmware.com/v1alpha1",
        "kind": "ModelEndpoint",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "pais-gitops",
                "pais.vmware.com/routing-name": endpoint_cfg["routing_name"].replace("/", "-").lower(),
            },
        },
        "spec": spec,
    }


def build_inference_gateway_route_crd(route_cfg: dict, default_namespace: str = "default") -> dict[str, Any]:
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
        backend_spec: dict[str, Any] = {
            "httpBaseUrl": backend_cfg["http_base_url"],
            "modelId": backend_cfg.get("model_id", "pais"),
        }
        if "tls" in backend_cfg:
            backend_spec["tls"] = backend_cfg["tls"]
        if "auth" in backend_cfg:
            backend_spec["auth"] = backend_cfg["auth"]
        spec["backend"] = backend_spec

    return {
        "apiVersion": "pais.vmware.com/v1alpha1",
        "kind": "InferenceGatewayRoute",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "pais-gitops",
            },
        },
        "spec": spec,
    }


# ---------------------------------------------------------------------------
# Manifest Export
# ---------------------------------------------------------------------------

def generate_k8s_manifests(config: dict) -> list[dict[str, Any]]:
    """
    Build all ModelEndpoint and InferenceGatewayRoute objects from config.
    """
    k8s_cfg = config.get("kubernetes", {})
    default_ns = k8s_cfg.get("namespace", "default")

    manifests: list[dict[str, Any]] = []

    for me in config.get("model_endpoints", []):
        manifests.append(build_model_endpoint_crd(me, default_namespace=default_ns))

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
# Kubectl Execution
# ---------------------------------------------------------------------------

def apply_k8s_manifests(manifests: list[dict[str, Any]], dry_run: bool = False) -> None:
    """
    Apply Kubernetes CRD manifests using `kubectl apply` if available.
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

    # Check if kubectl is available
    if not _is_kubectl_available():
        log.warning(
            "kubectl command is not available in environment. Skipping direct cluster apply. "
            "Manifests have been saved to disk for GitOps/manual deployment."
        )
        return

    log.info("Applying %d Kubernetes manifests via kubectl apply...", len(manifests))
    try:
        res = subprocess.run(
            ["kubectl", "apply", "-f", "-"],
            input=yaml_content,
            text=True,
            capture_output=True,
            check=True,
        )
        log.info("Kubectl apply output:\n%s", res.stdout.strip())
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"kubectl apply failed: {exc.stderr.strip()}") from exc


def delete_removed_k8s_resources(old_config: dict, new_config: dict, dry_run: bool = False) -> None:
    """
    Identify deleted ModelEndpoints and InferenceGatewayRoutes and remove them via kubectl.
    """
    old_endpoints = {me["name"]: me for me in old_config.get("model_endpoints", []) if isinstance(me, dict) and me.get("name")}
    new_endpoints = {me["name"]: me for me in new_config.get("model_endpoints", []) if isinstance(me, dict) and me.get("name")}
    removed_endpoints = set(old_endpoints) - set(new_endpoints)

    old_routes = {ir["name"]: ir for ir in old_config.get("inference_routes", []) if isinstance(ir, dict) and ir.get("name")}
    new_routes = {ir["name"]: ir for ir in new_config.get("inference_routes", []) if isinstance(ir, dict) and ir.get("name")}
    removed_routes = set(old_routes) - set(new_routes)

    if not removed_endpoints and not removed_routes:
        log.info("No Kubernetes ModelEndpoints or InferenceGatewayRoutes removed.")
        return

    default_ns = new_config.get("kubernetes", {}).get("namespace", "default")

    for name in sorted(removed_routes):
        route = old_endpoints.get(name) or old_routes.get(name) or {}
        ns = route.get("namespace", default_ns)
        log.info("Removing InferenceGatewayRoute '%s' in namespace '%s'...", name, ns)
        if dry_run:
            log.info("  [dry-run] kubectl delete inferencegatewayroute %s -n %s", name, ns)
        elif _is_kubectl_available():
            _run_kubectl(["delete", "inferencegatewayroute", name, "-n", ns, "--ignore-not-found=true"])

    for name in sorted(removed_endpoints):
        endpoint = old_endpoints.get(name) or {}
        ns = endpoint.get("namespace", default_ns)
        log.info("Removing ModelEndpoint '%s' in namespace '%s'...", name, ns)
        if dry_run:
            log.info("  [dry-run] kubectl delete modelendpoint %s -n %s", name, ns)
        elif _is_kubectl_available():
            _run_kubectl(["delete", "modelendpoint", name, "-n", ns, "--ignore-not-found=true"])


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
