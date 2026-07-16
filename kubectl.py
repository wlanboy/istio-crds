"""Kubernetes data collection — core: namespaces, CRDs, adoption."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

import urllib3
from kubernetes import client, config
from kubernetes.client import (
    V1CustomResourceDefinitionList,
    V1NamespaceList,
    V1ServiceList,
)

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def load_config(*, verify_ssl: bool = True) -> None:
    """Load kubeconfig (in-cluster first, then local ~/.kube/config).

    If ``verify_ssl`` is False, TLS certificate verification is disabled for
    all subsequent API calls (equivalent to ``kubectl``/``oc``
    ``--insecure-skip-tls-verify``) — useful against clusters with
    self-signed certificates.
    """
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    cfg = client.Configuration.get_default_copy()
    cfg.retries = 0  # pyright: ignore[reportAttributeAccessIssue]  # stub types this as None-only
    if not verify_ssl:
        cfg.verify_ssl = False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    client.Configuration.set_default(cfg)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Namespace listing
# ---------------------------------------------------------------------------

def get_namespaces() -> list[str]:
    v1 = client.CoreV1Api()
    ns_list = cast(V1NamespaceList, v1.list_namespace(_request_timeout=_REQUEST_TIMEOUT))
    return [ns.metadata.name for ns in (ns_list.items or [])]


# ---------------------------------------------------------------------------
# Service listing
# ---------------------------------------------------------------------------

@dataclass
class ServiceInfo:
    name: str
    namespace: str
    ports: list[int] = field(default_factory=list)
    selector: dict[str, str] = field(default_factory=dict)


def get_services(namespace: str | None = None) -> list[ServiceInfo]:
    """List Kubernetes Services, optionally restricted to one namespace.

    Used as the base service inventory for the Istio traffic graph — every
    Service is a potential mesh participant even if no VirtualService or
    DestinationRule references it.
    """
    v1 = client.CoreV1Api()
    if namespace is not None:
        svc_list = cast(
            V1ServiceList, v1.list_namespaced_service(namespace, _request_timeout=_REQUEST_TIMEOUT),
        )
    else:
        svc_list = cast(
            V1ServiceList, v1.list_service_for_all_namespaces(_request_timeout=_REQUEST_TIMEOUT),
        )
    return [
        ServiceInfo(
            name=svc.metadata.name,
            namespace=svc.metadata.namespace,
            ports=[p.port for p in (svc.spec.ports or [])] if svc.spec else [],
            selector=dict(svc.spec.selector or {}) if svc.spec else {},
        )
        for svc in (svc_list.items or [])
    ]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _custom_list(custom: client.CustomObjectsApi, *, group: str, version: str,
                 namespace: str | None, plural: str) -> dict[str, Any]:
    if namespace is not None:
        result = custom.list_namespaced_custom_object(
            group=group, version=version, namespace=namespace, plural=plural,
            _request_timeout=_REQUEST_TIMEOUT,
        )
    else:
        result = custom.list_cluster_custom_object(
            group=group, version=version, plural=plural,
            _request_timeout=_REQUEST_TIMEOUT,
        )
    return cast(dict[str, Any], result)


# ---------------------------------------------------------------------------
# CRD listing across all versions
# ---------------------------------------------------------------------------

@dataclass
class CRDVersionInfo:
    version: str
    served: bool
    storage: bool
    deprecated: bool = False
    deprecation_warning: str | None = None


@dataclass
class CRDVersionedInfo:
    name: str           # e.g. certificates.cert-manager.io
    group: str
    kind: str
    plural: str
    namespaced: bool
    versions: list[CRDVersionInfo] = field(default_factory=list)
    # Versions the API server still has objects persisted as (CRD status.storedVersions).
    stored_versions: list[str] = field(default_factory=list)
    # spec.conversion.strategy: "None" or "Webhook". Webhook conversion means
    # reading/writing non-storage versions depends on an external webhook being
    # reachable — worth flagging separately from the per-version served/storage flags.
    conversion_strategy: str = "None"
    # status.conditions[type=Established/NamesAccepted]. A CRD stuck at False here
    # never became usable (e.g. a names conflict) — it shows up in list_custom_resource_definition()
    # like any other CRD, but every API call against it will fail.
    established: bool = True
    names_accepted: bool = True
    established_message: str | None = None
    names_accepted_message: str | None = None

    @property
    def storage_version(self) -> str | None:
        return next((v.version for v in self.versions if v.storage), None)

    @property
    def pending_migration_versions(self) -> list[str]:
        """Stored versions other than the current storage version — objects still
        persisted under these have not been migrated and block their removal."""
        current = self.storage_version
        return [v for v in self.stored_versions if v != current]


def get_crd_versions(namespace: str | None = None) -> list[CRDVersionedInfo]:
    """List every CRD together with all of its API versions.

    If ``namespace`` is given, cluster-scoped CRDs are skipped (they have no
    per-namespace relevance); every namespaced CRD is still listed regardless
    of whether it actually has instances in that namespace.
    """
    ext = client.ApiextensionsV1Api()

    crd_list = cast(
        V1CustomResourceDefinitionList,
        ext.list_custom_resource_definition(_request_timeout=_REQUEST_TIMEOUT),
    )
    result: list[CRDVersionedInfo] = []

    for crd in (crd_list.items or []):
        spec = crd.spec
        is_namespaced = spec.scope == "Namespaced"

        if not is_namespaced and namespace is not None:
            continue

        status = getattr(crd, "status", None)
        conversion = getattr(spec, "conversion", None)
        conditions = {c.type: c for c in (getattr(status, "conditions", None) or [])}
        established_cond = conditions.get("Established")
        names_accepted_cond = conditions.get("NamesAccepted")
        info = CRDVersionedInfo(
            name=crd.metadata.name,
            group=spec.group,
            kind=spec.names.kind,
            plural=spec.names.plural,
            namespaced=is_namespaced,
            stored_versions=list(getattr(status, "stored_versions", None) or []),
            conversion_strategy=getattr(conversion, "strategy", None) or "None",
            established=established_cond is None or established_cond.status == "True",
            names_accepted=names_accepted_cond is None or names_accepted_cond.status == "True",
            established_message=established_cond.message
            if established_cond is not None and established_cond.status != "True" else None,
            names_accepted_message=names_accepted_cond.message
            if names_accepted_cond is not None and names_accepted_cond.status != "True" else None,
        )

        for v in (spec.versions or []):
            vinfo = CRDVersionInfo(
                version=v.name, served=v.served, storage=v.storage,
                deprecated=bool(getattr(v, "deprecated", False)),
                deprecation_warning=getattr(v, "deprecation_warning", None),
            )
            info.versions.append(vinfo)

        result.append(info)

    return sorted(result, key=lambda i: (i.group, i.kind))
