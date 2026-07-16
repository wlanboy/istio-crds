"""Istio data collection — fetch and parse the CRDs that describe traffic
routing and security policy: VirtualService, DestinationRule, Gateway,
ServiceEntry, Sidecar, WorkloadEntry, WorkloadGroup, PeerAuthentication,
AuthorizationPolicy and RequestAuthentication."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar, cast

import urllib3
from kubernetes import client
from kubernetes.client import V1CustomResourceDefinition
from kubernetes.client.rest import ApiException

from kubectl import _REQUEST_TIMEOUT, _custom_list

logger = logging.getLogger(__name__)

T = TypeVar("T")

_NETWORKING_GROUP = "networking.istio.io"
_SECURITY_GROUP = "security.istio.io"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class RouteDestination:
    protocol: str  # "http" | "tcp" | "tls" | "http-mirror" | "http-redirect"
    host: str
    subset: str | None = None
    port: int | None = None
    weight: int | None = None


@dataclass
class DelegateRef:
    name: str
    namespace: str


@dataclass
class VirtualServiceInfo:
    name: str
    namespace: str
    hosts: list[str] = field(default_factory=list)
    gateways: list[str] = field(default_factory=list)
    destinations: list[RouteDestination] = field(default_factory=list)
    delegates: list[DelegateRef] = field(default_factory=list)


@dataclass
class DestinationRuleInfo:
    name: str
    namespace: str
    host: str
    subsets: list[str] = field(default_factory=list)
    tls_mode: str | None = None


@dataclass
class GatewayServer:
    hosts: list[str]
    port_number: int | None
    protocol: str | None
    tls_mode: str | None


@dataclass
class GatewayInfo:
    name: str
    namespace: str
    selector: dict[str, str] = field(default_factory=dict)
    servers: list[GatewayServer] = field(default_factory=list)

    @property
    def direction(self) -> str:
        """Ingress/egress is only a naming convention in Istio (there is no
        formal field for it) — inferred from the selector used by the
        official Helm charts (istio: ingressgateway / egressgateway)."""
        if self.selector.get("istio") == "ingressgateway":
            return "ingress"
        if self.selector.get("istio") == "egressgateway":
            return "egress"
        return "custom"


@dataclass
class ServiceEntryEndpoint:
    address: str
    labels: dict[str, str] = field(default_factory=dict)
    ports: dict[str, int] = field(default_factory=dict)


@dataclass
class ServiceEntryInfo:
    name: str
    namespace: str
    hosts: list[str] = field(default_factory=list)
    location: str | None = None
    resolution: str | None = None
    ports: list[int] = field(default_factory=list)
    endpoints: list[ServiceEntryEndpoint] = field(default_factory=list)


@dataclass
class SidecarIngressRule:
    port_number: int | None
    protocol: str | None
    default_endpoint: str | None


@dataclass
class SidecarInfo:
    name: str
    namespace: str
    egress_hosts: list[str] = field(default_factory=list)
    ingress: list[SidecarIngressRule] = field(default_factory=list)


@dataclass
class WorkloadEntryInfo:
    name: str
    namespace: str
    address: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    service_account: str | None = None
    ports: dict[str, int] = field(default_factory=dict)


@dataclass
class WorkloadGroupInfo:
    name: str
    namespace: str
    labels: dict[str, str] = field(default_factory=dict)
    service_account: str | None = None
    ports: dict[str, int] = field(default_factory=dict)


@dataclass
class PeerAuthenticationInfo:
    name: str
    namespace: str
    mtls_mode: str | None
    has_selector: bool
    port_level_mtls: dict[str, str] = field(default_factory=dict)


@dataclass
class AuthorizationRule:
    from_namespaces: list[str] = field(default_factory=list)
    from_principals: list[str] = field(default_factory=list)
    to_hosts: list[str] = field(default_factory=list)


@dataclass
class TargetRef:
    kind: str
    name: str
    group: str | None = None


@dataclass
class AuthorizationPolicyInfo:
    name: str
    namespace: str
    action: str
    has_selector: bool
    rules: list[AuthorizationRule] = field(default_factory=list)
    target_refs: list[TargetRef] = field(default_factory=list)
    selector: dict[str, str] = field(default_factory=dict)


@dataclass
class RequestAuthenticationInfo:
    name: str
    namespace: str
    issuers: list[str] = field(default_factory=list)
    has_selector: bool = False


@dataclass
class IstioResources:
    virtual_services: list[VirtualServiceInfo] = field(default_factory=list)
    destination_rules: list[DestinationRuleInfo] = field(default_factory=list)
    gateways: list[GatewayInfo] = field(default_factory=list)
    service_entries: list[ServiceEntryInfo] = field(default_factory=list)
    sidecars: list[SidecarInfo] = field(default_factory=list)
    workload_entries: list[WorkloadEntryInfo] = field(default_factory=list)
    workload_groups: list[WorkloadGroupInfo] = field(default_factory=list)
    peer_authentications: list[PeerAuthenticationInfo] = field(default_factory=list)
    authorization_policies: list[AuthorizationPolicyInfo] = field(default_factory=list)
    request_authentications: list[RequestAuthenticationInfo] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Parsing (raw dict, as returned by CustomObjectsApi, -> dataclass)
# ---------------------------------------------------------------------------

def _meta(item: dict[str, Any]) -> tuple[str, str]:
    meta = item.get("metadata") or {}
    return meta.get("name", ""), meta.get("namespace", "")


def _parse_virtual_service(item: dict[str, Any]) -> VirtualServiceInfo:
    name, namespace = _meta(item)
    spec = item.get("spec") or {}
    destinations: list[RouteDestination] = []
    delegates: list[DelegateRef] = []
    for protocol in ("http", "tcp", "tls"):
        for route in spec.get(protocol) or []:
            for dest in route.get("route") or []:
                d = dest.get("destination") or {}
                host = d.get("host")
                if not host:
                    continue
                destinations.append(RouteDestination(
                    protocol=protocol,
                    host=host,
                    subset=d.get("subset"),
                    port=(d.get("port") or {}).get("number"),
                    weight=dest.get("weight"),
                ))
            if protocol != "http":
                continue

            delegate = route.get("delegate") or {}
            if delegate.get("name"):
                delegates.append(DelegateRef(
                    name=delegate["name"], namespace=delegate.get("namespace") or namespace,
                ))

            # `mirror` (singular, a bare Destination) and `mirrors` (list of
            # HTTPMirrorPolicy, each wrapping a "destination") are two
            # generations of the same shadow-traffic feature — normalize both
            # into destination-shaped dicts.
            for mirror in [route.get("mirror"), *(route.get("mirrors") or [])]:
                if not mirror:
                    continue
                d = mirror.get("destination") or mirror
                host = d.get("host")
                if not host:
                    continue
                destinations.append(RouteDestination(
                    protocol="http-mirror", host=host,
                    subset=d.get("subset"), port=(d.get("port") or {}).get("number"),
                ))

            redirect_authority = (route.get("redirect") or {}).get("authority")
            if redirect_authority:
                destinations.append(RouteDestination(protocol="http-redirect", host=redirect_authority))
    return VirtualServiceInfo(
        name=name,
        namespace=namespace,
        hosts=list(spec.get("hosts") or []),
        gateways=list(spec.get("gateways") or []),
        destinations=destinations,
        delegates=delegates,
    )


def _parse_destination_rule(item: dict[str, Any]) -> DestinationRuleInfo:
    name, namespace = _meta(item)
    spec = item.get("spec") or {}
    subsets = [s["name"] for s in (spec.get("subsets") or []) if s.get("name")]
    tls_mode = ((spec.get("trafficPolicy") or {}).get("tls") or {}).get("mode")
    return DestinationRuleInfo(
        name=name, namespace=namespace, host=spec.get("host", ""),
        subsets=subsets, tls_mode=tls_mode,
    )


def _parse_gateway(item: dict[str, Any]) -> GatewayInfo:
    name, namespace = _meta(item)
    spec = item.get("spec") or {}
    servers = []
    for s in spec.get("servers") or []:
        port = s.get("port") or {}
        tls = s.get("tls") or {}
        servers.append(GatewayServer(
            hosts=list(s.get("hosts") or []),
            port_number=port.get("number"),
            protocol=port.get("protocol"),
            tls_mode=tls.get("mode"),
        ))
    return GatewayInfo(
        name=name, namespace=namespace,
        selector=dict(spec.get("selector") or {}), servers=servers,
    )


def _parse_service_entry(item: dict[str, Any]) -> ServiceEntryInfo:
    name, namespace = _meta(item)
    spec = item.get("spec") or {}
    ports = [p["number"] for p in (spec.get("ports") or []) if p.get("number")]
    endpoints = [
        ServiceEntryEndpoint(
            address=ep["address"], labels=dict(ep.get("labels") or {}),
            ports=dict(ep.get("ports") or {}),
        )
        for ep in (spec.get("endpoints") or []) if ep.get("address")
    ]
    return ServiceEntryInfo(
        name=name, namespace=namespace, hosts=list(spec.get("hosts") or []),
        location=spec.get("location"), resolution=spec.get("resolution"), ports=ports,
        endpoints=endpoints,
    )


def _parse_sidecar(item: dict[str, Any]) -> SidecarInfo:
    name, namespace = _meta(item)
    spec = item.get("spec") or {}
    egress_hosts = [
        host
        for egress in (spec.get("egress") or [])
        for host in (egress.get("hosts") or [])
    ]
    ingress = [
        SidecarIngressRule(
            port_number=(ing.get("port") or {}).get("number"),
            protocol=(ing.get("port") or {}).get("protocol"),
            default_endpoint=ing.get("defaultEndpoint"),
        )
        for ing in (spec.get("ingress") or [])
    ]
    return SidecarInfo(name=name, namespace=namespace, egress_hosts=egress_hosts, ingress=ingress)


def _parse_workload_entry(item: dict[str, Any]) -> WorkloadEntryInfo:
    name, namespace = _meta(item)
    spec = item.get("spec") or {}
    return WorkloadEntryInfo(
        name=name, namespace=namespace, address=spec.get("address"),
        labels=dict(spec.get("labels") or {}), service_account=spec.get("serviceAccount"),
        ports=dict(spec.get("ports") or {}),
    )


def _parse_workload_group(item: dict[str, Any]) -> WorkloadGroupInfo:
    name, namespace = _meta(item)
    spec = item.get("spec") or {}
    template = spec.get("template") or {}
    return WorkloadGroupInfo(
        name=name, namespace=namespace,
        labels=dict((spec.get("metadata") or {}).get("labels") or {}),
        service_account=template.get("serviceAccount"),
        ports=dict(template.get("ports") or {}),
    )


def _parse_peer_authentication(item: dict[str, Any]) -> PeerAuthenticationInfo:
    name, namespace = _meta(item)
    spec = item.get("spec") or {}
    port_level_mtls = {
        str(port): (entry or {}).get("mode", "")
        for port, entry in (spec.get("portLevelMtls") or {}).items()
    }
    return PeerAuthenticationInfo(
        name=name, namespace=namespace,
        mtls_mode=(spec.get("mtls") or {}).get("mode"),
        has_selector=bool(spec.get("selector")),
        port_level_mtls=port_level_mtls,
    )


def _parse_authorization_policy(item: dict[str, Any]) -> AuthorizationPolicyInfo:
    name, namespace = _meta(item)
    spec = item.get("spec") or {}
    rules: list[AuthorizationRule] = []
    for rule in spec.get("rules") or []:
        from_namespaces: list[str] = []
        from_principals: list[str] = []
        for f in rule.get("from") or []:
            source = f.get("source") or {}
            from_namespaces += source.get("namespaces") or []
            from_principals += source.get("principals") or []
        to_hosts: list[str] = [
            host
            for t in (rule.get("to") or [])
            for host in ((t.get("operation") or {}).get("hosts") or [])
        ]
        rules.append(AuthorizationRule(
            from_namespaces=from_namespaces, from_principals=from_principals, to_hosts=to_hosts,
        ))
    raw_refs = [spec["targetRef"]] if spec.get("targetRef") else []
    raw_refs += spec.get("targetRefs") or []
    target_refs = [
        TargetRef(kind=r.get("kind", ""), name=r["name"], group=r.get("group") or None)
        for r in raw_refs if r.get("name")
    ]
    # `spec.selector` is a WorkloadSelector (`{matchLabels: {...}}`), unlike
    # e.g. Gateway's plain-map `spec.selector` — unwrap it so graph.py can
    # match it against a Service's/Gateway's own selector.
    selector = dict((spec.get("selector") or {}).get("matchLabels") or {})
    return AuthorizationPolicyInfo(
        name=name, namespace=namespace, action=spec.get("action") or "ALLOW",
        has_selector=bool(spec.get("selector") or raw_refs),
        rules=rules,
        target_refs=target_refs,
        selector=selector,
    )


def _parse_request_authentication(item: dict[str, Any]) -> RequestAuthenticationInfo:
    name, namespace = _meta(item)
    spec = item.get("spec") or {}
    issuers = [j["issuer"] for j in (spec.get("jwtRules") or []) if j.get("issuer")]
    return RequestAuthenticationInfo(
        name=name, namespace=namespace, issuers=issuers,
        has_selector=bool(spec.get("selector") or spec.get("targetRefs")),
    )


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------

def _served_version(ext: client.ApiextensionsV1Api, crd_name: str) -> str | None:
    try:
        crd = cast(
            V1CustomResourceDefinition,
            ext.read_custom_resource_definition(crd_name, _request_timeout=_REQUEST_TIMEOUT),
        )
    except (ApiException, urllib3.exceptions.HTTPError) as e:
        logger.debug("CRD %s not available: %s", crd_name, e)
        return None
    served = [v for v in ((crd.spec.versions if crd.spec else None) or []) if v.served]
    storage = next((v.name for v in served if v.storage), None)
    return storage or (served[0].name if served else None)


def _fetch(
    ext: client.ApiextensionsV1Api, custom: client.CustomObjectsApi, *,
    group: str, plural: str, namespace: str | None, parser: Callable[[dict[str, Any]], T],
) -> list[T]:
    version = _served_version(ext, f"{plural}.{group}")
    if version is None:
        return []
    try:
        resp = _custom_list(custom, group=group, version=version, namespace=namespace, plural=plural)
    except (ApiException, urllib3.exceptions.HTTPError) as e:
        logger.debug("Failed to list %s/%s %s: %s", group, version, plural, e)
        return []
    return [parser(item) for item in resp.get("items", [])]


def get_istio_resources(namespace: str | None = None) -> IstioResources:
    """Fetch every traffic-routing and security-policy Istio CRD.

    If ``namespace`` is given, only that namespace is scanned; otherwise every
    namespace in the cluster is included. CRD types that aren't installed
    (e.g. Istio not present, or an older/newer CRD version) are silently
    skipped rather than failing the whole fetch.
    """
    ext = client.ApiextensionsV1Api()
    custom = client.CustomObjectsApi()

    return IstioResources(
        virtual_services=_fetch(
            ext, custom, group=_NETWORKING_GROUP, plural="virtualservices",
            namespace=namespace, parser=_parse_virtual_service,
        ),
        destination_rules=_fetch(
            ext, custom, group=_NETWORKING_GROUP, plural="destinationrules",
            namespace=namespace, parser=_parse_destination_rule,
        ),
        gateways=_fetch(
            ext, custom, group=_NETWORKING_GROUP, plural="gateways",
            namespace=namespace, parser=_parse_gateway,
        ),
        service_entries=_fetch(
            ext, custom, group=_NETWORKING_GROUP, plural="serviceentries",
            namespace=namespace, parser=_parse_service_entry,
        ),
        sidecars=_fetch(
            ext, custom, group=_NETWORKING_GROUP, plural="sidecars",
            namespace=namespace, parser=_parse_sidecar,
        ),
        workload_entries=_fetch(
            ext, custom, group=_NETWORKING_GROUP, plural="workloadentries",
            namespace=namespace, parser=_parse_workload_entry,
        ),
        workload_groups=_fetch(
            ext, custom, group=_NETWORKING_GROUP, plural="workloadgroups",
            namespace=namespace, parser=_parse_workload_group,
        ),
        peer_authentications=_fetch(
            ext, custom, group=_SECURITY_GROUP, plural="peerauthentications",
            namespace=namespace, parser=_parse_peer_authentication,
        ),
        authorization_policies=_fetch(
            ext, custom, group=_SECURITY_GROUP, plural="authorizationpolicies",
            namespace=namespace, parser=_parse_authorization_policy,
        ),
        request_authentications=_fetch(
            ext, custom, group=_SECURITY_GROUP, plural="requestauthentications",
            namespace=namespace, parser=_parse_request_authentication,
        ),
    )
