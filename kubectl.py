"""Kubernetes data collection — core: namespaces, CRDs, adoption."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

import urllib3
import yaml
from kubernetes import client, config
from kubernetes.client import (
    V1ConfigMap,
    V1CustomResourceDefinitionList,
    V1DeploymentList,
    V1Namespace,
    V1NamespaceList,
    V1NetworkPolicyList,
    V1PodList,
    V1ServiceAccountList,
    V1ServiceList,
)
from kubernetes.client.rest import ApiException

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 30
_DEFAULT_ROOT_NAMESPACE = "istio-system"


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

@dataclass
class NamespaceInfo:
    name: str
    labels: dict[str, str] = field(default_factory=dict)


def _namespace_info(ns: V1Namespace) -> NamespaceInfo:
    meta = ns.metadata
    return NamespaceInfo(
        name=meta.name if meta and meta.name else "",
        labels=dict(meta.labels or {}) if meta else {},
    )


def get_namespaces(namespace: str | None = None) -> list[NamespaceInfo]:
    """List Kubernetes namespaces including their labels, optionally restricted
    to a single namespace.

    Labels (e.g. ``istio-injection: enabled``, ``istio.io/rev``) are what
    determine whether a namespace's pods actually get a sidecar and are part
    of the mesh in the first place — a namespace-scoped policy targeting a
    namespace without one of these labels never actually applies to anything.

    Listing all namespaces (``namespace=None``) requires cluster-scoped
    ``list namespaces`` RBAC. If ``namespace`` is given, only that namespace
    is read via ``read_namespace()`` instead — a ``get`` on ``namespaces``
    scoped to that one name (e.g. via ``resourceNames``) is enough for that,
    without needing to list every namespace in the cluster.
    """
    v1 = client.CoreV1Api()
    if namespace is not None:
        ns = cast(V1Namespace, v1.read_namespace(namespace, _request_timeout=_REQUEST_TIMEOUT))
        return [_namespace_info(ns)]
    ns_list = cast(V1NamespaceList, v1.list_namespace(_request_timeout=_REQUEST_TIMEOUT))
    return [_namespace_info(ns) for ns in (ns_list.items or [])]


# ---------------------------------------------------------------------------
# Mesh config (root namespace)
# ---------------------------------------------------------------------------

def get_mesh_root_namespace(istio_namespace: str = _DEFAULT_ROOT_NAMESPACE) -> str:
    """Resolve the mesh's root namespace from the control plane's "istio"
    ConfigMap (``data["mesh"].rootNamespace``).

    Namespace-less PeerAuthentication/AuthorizationPolicy/RequestAuthentication/
    Sidecar resources only apply mesh-wide when they live in this namespace —
    everywhere else the same resource is only namespace-scoped. It defaults to
    "istio-system" but is configurable via ``meshConfig.rootNamespace``, so it
    can't just be assumed.
    """
    v1 = client.CoreV1Api()
    try:
        cm = cast(
            V1ConfigMap,
            v1.read_namespaced_config_map(
                "istio", istio_namespace, _request_timeout=_REQUEST_TIMEOUT,
            ),
        )
    except (ApiException, urllib3.exceptions.HTTPError) as e:
        logger.debug("Could not read istio ConfigMap in %s: %s", istio_namespace, e)
        return istio_namespace

    mesh_yaml = (cm.data or {}).get("mesh")
    if not mesh_yaml:
        return istio_namespace
    try:
        mesh = yaml.safe_load(mesh_yaml) or {}
    except yaml.YAMLError as e:
        logger.debug("Could not parse mesh config in %s: %s", istio_namespace, e)
        return istio_namespace
    return mesh.get("rootNamespace") or istio_namespace


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
# ServiceAccount listing
# ---------------------------------------------------------------------------

@dataclass
class ServiceAccountInfo:
    name: str
    namespace: str


def get_service_accounts(namespace: str | None = None) -> list[ServiceAccountInfo]:
    """List Kubernetes ServiceAccounts, optionally restricted to one namespace.

    Used to resolve the SPIFFE principals referenced by
    AuthorizationPolicy.rules[].from.source.principals and the serviceAccount
    fields on WorkloadEntry/WorkloadGroup/Pod to actual cluster objects.
    """
    v1 = client.CoreV1Api()
    if namespace is not None:
        sa_list = cast(
            V1ServiceAccountList,
            v1.list_namespaced_service_account(namespace, _request_timeout=_REQUEST_TIMEOUT),
        )
    else:
        sa_list = cast(
            V1ServiceAccountList,
            v1.list_service_account_for_all_namespaces(_request_timeout=_REQUEST_TIMEOUT),
        )
    return [
        ServiceAccountInfo(name=sa.metadata.name, namespace=sa.metadata.namespace)
        for sa in (sa_list.items or [])
    ]


# ---------------------------------------------------------------------------
# Pod listing
# ---------------------------------------------------------------------------

@dataclass
class PodInfo:
    name: str
    namespace: str
    labels: dict[str, str] = field(default_factory=dict)
    service_account: str | None = None


def get_pods(namespace: str | None = None) -> list[PodInfo]:
    """List Kubernetes Pods, optionally restricted to one namespace.

    This is the resolution target for every label selector in the Istio
    graph (Service.spec.selector, Gateway/Sidecar/PeerAuthentication/
    AuthorizationPolicy/RequestAuthentication selectors) and the missing link
    between a Service and the ServiceAccount that backs it.
    """
    v1 = client.CoreV1Api()
    if namespace is not None:
        pod_list = cast(
            V1PodList, v1.list_namespaced_pod(namespace, _request_timeout=_REQUEST_TIMEOUT),
        )
    else:
        pod_list = cast(
            V1PodList, v1.list_pod_for_all_namespaces(_request_timeout=_REQUEST_TIMEOUT),
        )
    return [
        PodInfo(
            name=pod.metadata.name,
            namespace=pod.metadata.namespace,
            labels=dict(pod.metadata.labels or {}),
            service_account=pod.spec.service_account_name if pod.spec else None,
        )
        for pod in (pod_list.items or [])
    ]


# ---------------------------------------------------------------------------
# Deployment listing
# ---------------------------------------------------------------------------

@dataclass
class DeploymentInfo:
    name: str
    namespace: str
    labels: dict[str, str] = field(default_factory=dict)
    service_account: str | None = None


def get_deployments(namespace: str | None = None) -> list[DeploymentInfo]:
    """List Kubernetes Deployments, optionally restricted to one namespace.

    ``labels`` are the pod template's labels (``spec.template.metadata.labels``)
    rather than ``spec.selector.matchLabels`` — it's the template labels that
    actual Service/Sidecar/AuthorizationPolicy selectors are matched against,
    since those match live Pod labels, not the Deployment's own (typically
    narrower) selector.
    """
    apps = client.AppsV1Api()
    if namespace is not None:
        dep_list = cast(
            V1DeploymentList,
            apps.list_namespaced_deployment(namespace, _request_timeout=_REQUEST_TIMEOUT),
        )
    else:
        dep_list = cast(
            V1DeploymentList, apps.list_deployment_for_all_namespaces(_request_timeout=_REQUEST_TIMEOUT),
        )
    result: list[DeploymentInfo] = []
    for dep in (dep_list.items or []):
        template = dep.spec.template if dep.spec else None
        template_meta = template.metadata if template else None
        template_spec = template.spec if template else None
        result.append(DeploymentInfo(
            name=dep.metadata.name,
            namespace=dep.metadata.namespace,
            labels=dict(template_meta.labels or {}) if template_meta else {},
            service_account=template_spec.service_account_name if template_spec else None,
        ))
    return result


# ---------------------------------------------------------------------------
# NetworkPolicy listing
# ---------------------------------------------------------------------------

@dataclass
class NetworkPolicyPeer:
    pod_selector: dict[str, str] = field(default_factory=dict)
    namespace_selector: dict[str, str] = field(default_factory=dict)
    ip_block_cidr: str | None = None


@dataclass
class NetworkPolicyPort:
    protocol: str | None = None
    port: str | None = None
    end_port: int | None = None


@dataclass
class NetworkPolicyRule:
    peers: list[NetworkPolicyPeer] = field(default_factory=list)
    ports: list[NetworkPolicyPort] = field(default_factory=list)


@dataclass
class NetworkPolicyInfo:
    name: str
    namespace: str
    pod_selector: dict[str, str] = field(default_factory=dict)
    policy_types: list[str] = field(default_factory=list)
    ingress: list[NetworkPolicyRule] = field(default_factory=list)
    egress: list[NetworkPolicyRule] = field(default_factory=list)


def _network_policy_peer(peer: Any) -> NetworkPolicyPeer:
    pod_selector = getattr(peer, "pod_selector", None)
    namespace_selector = getattr(peer, "namespace_selector", None)
    ip_block = getattr(peer, "ip_block", None)
    return NetworkPolicyPeer(
        pod_selector=dict(pod_selector.match_labels or {}) if pod_selector else {},
        namespace_selector=dict(namespace_selector.match_labels or {}) if namespace_selector else {},
        ip_block_cidr=getattr(ip_block, "cidr", None) if ip_block else None,
    )


def _network_policy_port(port: Any) -> NetworkPolicyPort:
    return NetworkPolicyPort(
        protocol=getattr(port, "protocol", None),
        port=str(getattr(port, "port", None)) if getattr(port, "port", None) is not None else None,
        end_port=getattr(port, "end_port", None),
    )


def get_network_policies(namespace: str | None = None) -> list[NetworkPolicyInfo]:
    """List Kubernetes NetworkPolicies, optionally restricted to one namespace.

    NetworkPolicies gate L3/L4 traffic independently of and beneath Istio's
    AuthorizationPolicy/Sidecar layer — a pod-to-pod edge the Istio config
    otherwise allows can still be dropped here, so this is required input for
    the "which edges are actually allowed" step of the traffic graph.
    """
    net = client.NetworkingV1Api()
    if namespace is not None:
        np_list = cast(
            V1NetworkPolicyList,
            net.list_namespaced_network_policy(namespace, _request_timeout=_REQUEST_TIMEOUT),
        )
    else:
        np_list = cast(
            V1NetworkPolicyList,
            net.list_network_policy_for_all_namespaces(_request_timeout=_REQUEST_TIMEOUT),
        )
    result: list[NetworkPolicyInfo] = []
    for np in (np_list.items or []):
        spec = np.spec
        ingress = [
            NetworkPolicyRule(
                peers=[_network_policy_peer(p) for p in (getattr(rule, "_from", None) or [])],
                ports=[_network_policy_port(p) for p in (rule.ports or [])],
            )
            for rule in (getattr(spec, "ingress", None) or [])
        ]
        egress = [
            NetworkPolicyRule(
                peers=[_network_policy_peer(p) for p in (rule.to or [])],
                ports=[_network_policy_port(p) for p in (rule.ports or [])],
            )
            for rule in (getattr(spec, "egress", None) or [])
        ]
        result.append(NetworkPolicyInfo(
            name=np.metadata.name,
            namespace=np.metadata.namespace,
            pod_selector=dict(spec.pod_selector.match_labels or {}) if spec.pod_selector else {},
            policy_types=list(spec.policy_types or []),
            ingress=ingress,
            egress=egress,
        ))
    return result


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
