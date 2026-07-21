"""CLI: Erstellt einen JSON-Abhängigkeitsgraphen (Knoten + Kanten) aus allen
erfassten Kubernetes-/Istio-Objekten.

Während istio-objekt-liste.py jedes Objekt flach ausgibt, löst dieses Skript
die Beziehungen *zwischen* ihnen auf – Label-Selektoren werden gegen Pods
abgeglichen, Route-/Host-Strings gegen Services, SPIFFE-Principals gegen
ServiceAccounts, targetRefs gegen benannte Ressourcen – und erzeugt daraus
eine explizite Kantenliste, sodass das Ergebnis direkt in einen
Graph-Renderer (z. B. D3, Graphviz, Cytoscape) eingespeist werden kann,
anstatt diese Verknüpfungen erneut aus den Rohspezifikationen ableiten zu
müssen.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import sys
from dataclasses import dataclass, field

import urllib3
from kubernetes.client.rest import ApiException
from kubernetes.config import ConfigException

from istio import HostInfo, IstioResources, TargetRef, WorkloadEntryInfo, get_hosts, get_istio_resources
from kubectl import (
    NamespaceInfo,
    NetworkPolicyInfo,
    NetworkPolicyPeer,
    PodInfo,
    ServiceAccountInfo,
    ServiceInfo,
    get_mesh_root_namespace,
    get_namespaces,
    get_network_policies,
    get_pods,
    get_service_accounts,
    get_services,
    load_config,
)

logger = logging.getLogger(__name__)

# SPIFFE-Principal, z. B. "cluster.local/ns/default/sa/httpbin".
_PRINCIPAL_RE = re.compile(r"/ns/(?P<namespace>[^/]+)/sa/(?P<name>[^/]+)$")

# TargetRef.kind -> unten verwendetes Knotenart-Präfix, für die targetRefs
# von AuthorizationPolicy/RequestAuthentication.
_TARGET_REF_KIND = {
    "Gateway": "gateway",
    "Service": "service",
    "VirtualService": "virtualservice",
    "ServiceEntry": "serviceentry",
    "Sidecar": "sidecar",
    "WorkloadGroup": "workloadgroup",
}


# ---------------------------------------------------------------------------
# Graph-Modell
# ---------------------------------------------------------------------------

@dataclass
class Node:
    id: str
    kind: str
    name: str
    namespace: str | None = None
    attributes: dict[str, object] = field(default_factory=dict)


@dataclass
class Edge:
    source: str
    target: str
    relation: str
    attributes: dict[str, object] = field(default_factory=dict)


class GraphBuilder:
    """Sammelt Knoten und Kanten und löst Label-Selektoren / Host-Strings /
    Principals zu konkreten Kanten zwischen ihnen auf."""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: list[Edge] = []

    def add_node(
        self, kind: str, name: str, namespace: str | None = None, **attributes: object,
    ) -> str:
        node_id = f"{kind}:{namespace}/{name}" if namespace else f"{kind}:{name}"
        if node_id not in self._nodes:
            self._nodes[node_id] = Node(
                id=node_id, kind=kind, name=name, namespace=namespace, attributes=attributes,
            )
        return node_id

    def add_edge(self, source: str, target: str, relation: str, **attributes: object) -> None:
        # Kanten werden nur zwischen tatsächlich existierenden Knoten erzeugt,
        # damit ein fehlerhafter targetRef oder ein nicht auflösbarer Host
        # keine hängende Kante erzeugt.
        if source not in self._nodes or target not in self._nodes:
            logger.debug("Kante %s -%s-> %s wird übersprungen: Endpunkt nicht gefunden", source, relation, target)
            return
        self._edges.append(Edge(source=source, target=target, relation=relation, attributes=attributes))

    def build(self) -> dict[str, object]:
        return {
            "nodes": [dataclasses.asdict(n) for n in sorted(self._nodes.values(), key=lambda n: n.id)],
            "edges": [dataclasses.asdict(e) for e in self._edges],
        }


# ---------------------------------------------------------------------------
# Hilfsfunktionen zur Auflösung von Selektoren/Referenzen
# ---------------------------------------------------------------------------

def _selector_matches(selector: dict[str, str], labels: dict[str, str]) -> bool:
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


def _host_matches_service(host: str, svc: ServiceInfo) -> bool:
    """Gleicht einen Host-String aus VirtualService/DestinationRule/... gegen
    den Cluster-DNS-Namen eines Service ab, in jeder seiner Kurz-/FQDN-Formen."""
    if not host or host == "*":
        return False
    short = f"{svc.name}.{svc.namespace}"
    return host == svc.name or host == short or host.startswith(f"{short}.")


def _parse_principal(principal: str) -> tuple[str, str] | None:
    m = _PRINCIPAL_RE.search(principal)
    return (m.group("namespace"), m.group("name")) if m else None


# ---------------------------------------------------------------------------
# Erzeugung der Knoten
# ---------------------------------------------------------------------------

def _add_core_nodes(
    g: GraphBuilder, *, namespaces: list[NamespaceInfo], services: list[ServiceInfo],
    service_accounts: list[ServiceAccountInfo], pods: list[PodInfo],
    network_policies: list[NetworkPolicyInfo], hosts: list[HostInfo], mesh_root_namespace: str,
) -> None:
    for ns in namespaces:
        g.add_node("namespace", ns.name, labels=ns.labels, mesh_root=ns.name == mesh_root_namespace)

    for svc in services:
        node_id = g.add_node("service", svc.name, svc.namespace, ports=svc.ports, selector=svc.selector)
        g.add_edge(node_id, g.add_node("namespace", svc.namespace), "in_namespace")

    for sa in service_accounts:
        node_id = g.add_node("serviceaccount", sa.name, sa.namespace)
        g.add_edge(node_id, g.add_node("namespace", sa.namespace), "in_namespace")

    for pod in pods:
        node_id = g.add_node("pod", pod.name, pod.namespace, labels=pod.labels)
        g.add_edge(node_id, g.add_node("namespace", pod.namespace), "in_namespace")
        if pod.service_account:
            sa_id = f"serviceaccount:{pod.namespace}/{pod.service_account}"
            g.add_edge(node_id, sa_id, "uses_service_account")

    for np in network_policies:
        node_id = g.add_node(
            "networkpolicy", np.name, np.namespace, policy_types=np.policy_types,
        )
        g.add_edge(node_id, g.add_node("namespace", np.namespace), "in_namespace")

    for host in hosts:
        g.add_node("host", host.host)


def _add_service_pod_edges(
    g: GraphBuilder, *, services: list[ServiceInfo], pods: list[PodInfo],
    workload_entries: list[WorkloadEntryInfo],
) -> None:
    # Service.spec.selector passt nicht nur auf Pod-Labels, sondern auch auf
    # WorkloadEntry.spec.labels — darüber treten VM-/Bare-Metal-Workloads dem
    # Mesh bei, als wären sie Pods.
    for svc in services:
        svc_id = f"service:{svc.namespace}/{svc.name}"
        for pod in pods:
            if pod.namespace == svc.namespace and _selector_matches(svc.selector, pod.labels):
                g.add_edge(svc_id, f"pod:{pod.namespace}/{pod.name}", "selects")
        for we in workload_entries:
            if we.namespace == svc.namespace and _selector_matches(svc.selector, we.labels):
                g.add_edge(svc_id, f"workloadentry:{we.namespace}/{we.name}", "selects")


def _add_host_service_edges(g: GraphBuilder, *, hosts: list[HostInfo], services: list[ServiceInfo]) -> None:
    for host in hosts:
        host_id = f"host:{host.host}"
        for svc in services:
            if _host_matches_service(host.host, svc):
                g.add_edge(host_id, f"service:{svc.namespace}/{svc.name}", "resolves_to")


def _workload_scope_edges(
    g: GraphBuilder, *, node_id: str, namespace: str, selector: dict[str, str], pods: list[PodInfo],
    workload_entries: list[WorkloadEntryInfo] | None = None,
    namespaces: list[NamespaceInfo] | None = None, mesh_root_namespace: str | None = None,
) -> None:
    """Verknüpft ein Policy-/Konfigurationsobjekt mit den Pods (und, sofern
    angegeben, WorkloadEntries) auf die sein Workload-Selektor passt, oder –
    falls der Selektor leer ist – mit seinem eigenen Namespace.

    Ist der Selektor leer *und* liegt das Objekt im Mesh-Root-Namespace, gilt
    es laut Istios Selektor-Semantik Mesh-weit statt nur für den eigenen
    Namespace (nur relevant für PeerAuthentication/RequestAuthentication/
    AuthorizationPolicy/Sidecar; `namespaces`/`mesh_root_namespace` daher nur
    von diesen Aufrufstellen übergeben)."""
    if not selector:
        if namespaces is not None and namespace == mesh_root_namespace:
            for ns in namespaces:
                g.add_edge(node_id, f"namespace:{ns.name}", "applies_to_namespace", mesh_wide=True)
        else:
            g.add_edge(node_id, f"namespace:{namespace}", "applies_to_namespace")
        return
    for pod in pods:
        if pod.namespace == namespace and _selector_matches(selector, pod.labels):
            g.add_edge(node_id, f"pod:{pod.namespace}/{pod.name}", "applies_to")
    for we in workload_entries or []:
        if we.namespace == namespace and _selector_matches(selector, we.labels):
            g.add_edge(node_id, f"workloadentry:{we.namespace}/{we.name}", "applies_to")


def _gateway_selector_edges(
    g: GraphBuilder, *, node_id: str, selector: dict[str, str], pods: list[PodInfo],
    workload_entries: list[WorkloadEntryInfo],
) -> None:
    """Gateway.selector passt standardmäßig auf Proxy-Workloads über *alle*
    Namespaces hinweg (PILOT_SCOPE_GATEWAY_TO_NAMESPACE=false ist der Default
    von istiod) – im Gegensatz zu den Selektoren von Sidecar/PeerAuthentication/
    AuthorizationPolicy/RequestAuthentication, die immer auf den eigenen
    Namespace des Objekts beschränkt sind. Ein Gateway wird häufig in einem
    Anwendungs-Namespace definiert, selektiert dabei aber die gemeinsam
    genutzten ingressgateway-Pods, die z. B. in istio-system/istio-ingress
    laufen."""
    for pod in pods:
        if _selector_matches(selector, pod.labels):
            g.add_edge(node_id, f"pod:{pod.namespace}/{pod.name}", "selects")
    for we in workload_entries:
        if _selector_matches(selector, we.labels):
            g.add_edge(node_id, f"workloadentry:{we.namespace}/{we.name}", "selects")


def _add_export_to_edges(
    g: GraphBuilder, *, node_id: str, namespace: str, export_to: list[str], namespaces: list[NamespaceInfo],
) -> None:
    """exportTo schränkt ein, welche Namespaces dieses Objekt referenzieren
    dürfen: "." steht für den eigenen Namespace, "*" (bzw. eine leere Liste –
    Istios Default) für alle Namespaces, jeder andere Eintrag benennt einen
    Namespace explizit."""
    if not export_to or "*" in export_to:
        for ns in namespaces:
            g.add_edge(node_id, f"namespace:{ns.name}", "exported_to")
        return
    for entry in export_to:
        target_namespace = namespace if entry == "." else entry
        g.add_edge(node_id, f"namespace:{target_namespace}", "exported_to")


def _add_target_ref_edges(g: GraphBuilder, *, node_id: str, namespace: str, target_refs: list[TargetRef]) -> None:
    for ref in target_refs:
        prefix = _TARGET_REF_KIND.get(ref.kind)
        if prefix is None:
            logger.debug("Nicht behandelte targetRef-Art %s bei %s", ref.kind, node_id)
            continue
        g.add_edge(node_id, f"{prefix}:{namespace}/{ref.name}", "targets")


def _add_istio_nodes(g: GraphBuilder, *, resources: IstioResources) -> None:
    # Für jedes Istio-Objekt werden die Knoten vorab in einem Durchlauf
    # angelegt, damit sich Querverweise weiter unten (z. B. ein VirtualService,
    # das an ein später in der Liste definiertes Gateway angehängt ist) immer
    # auflösen lassen – unabhängig von der Deklarationsreihenfolge.
    for vs in resources.virtual_services:
        g.add_node("virtualservice", vs.name, vs.namespace, export_to=vs.export_to)
    for dr in resources.destination_rules:
        g.add_node(
            "destinationrule", dr.name, dr.namespace,
            subsets=[s.name for s in dr.subsets], tls_mode=dr.tls_mode, ports=dr.ports,
            export_to=dr.export_to,
        )
    for gw in resources.gateways:
        g.add_node("gateway", gw.name, gw.namespace, direction=gw.direction, selector=gw.selector)
    for se in resources.service_entries:
        g.add_node(
            "serviceentry", se.name, se.namespace,
            location=se.location, resolution=se.resolution, export_to=se.export_to,
        )
    for sc in resources.sidecars:
        g.add_node("sidecar", sc.name, sc.namespace)
    for we in resources.workload_entries:
        g.add_node("workloadentry", we.name, we.namespace, address=we.address, labels=we.labels)
    for wg in resources.workload_groups:
        g.add_node("workloadgroup", wg.name, wg.namespace, labels=wg.labels)
    for pa in resources.peer_authentications:
        g.add_node("peerauthentication", pa.name, pa.namespace, mtls_mode=pa.mtls_mode)
    for ap in resources.authorization_policies:
        g.add_node("authorizationpolicy", ap.name, ap.namespace, action=ap.action)
    for ra in resources.request_authentications:
        g.add_node("requestauthentication", ra.name, ra.namespace, issuers=ra.issuers)


def _add_istio_edges(
    g: GraphBuilder, *, resources: IstioResources, pods: list[PodInfo],
    namespaces: list[NamespaceInfo], mesh_root_namespace: str,
) -> None:
    # Verknüpft die zuvor in _add_istio_nodes angelegten Istio-Knoten
    # untereinander sowie mit Namespaces, Hosts, ServiceAccounts und Pods.
    # Die Kanten werden getrennt von der Knotenerzeugung aufgebaut, damit
    # beim Verweisen auf andere Istio-Objekte (z. B. VirtualService -> Gateway)
    # der Zielknoten bereits existiert, unabhängig von der Reihenfolge der
    # Ressourcen in resources.
    for vs in resources.virtual_services:
        node_id = f"virtualservice:{vs.namespace}/{vs.name}"
        g.add_edge(node_id, f"namespace:{vs.namespace}", "in_namespace")
        _add_export_to_edges(
            g, node_id=node_id, namespace=vs.namespace, export_to=vs.export_to, namespaces=namespaces,
        )
        for host in vs.hosts:
            g.add_edge(node_id, f"host:{host}", "applies_to_host")
        for gateway in vs.gateways:
            if gateway in ("mesh", ""):
                continue
            gw_namespace, _, gw_name = gateway.rpartition("/")
            g.add_edge(node_id, f"gateway:{gw_namespace or vs.namespace}/{gw_name}", "attached_to_gateway")
        for dest in vs.destinations:
            g.add_edge(
                node_id, f"host:{dest.host}", "routes_to",
                protocol=dest.protocol, subset=dest.subset, port=dest.port, weight=dest.weight,
            )
        for delegate in vs.delegates:
            g.add_edge(node_id, f"virtualservice:{delegate.namespace}/{delegate.name}", "delegates_to")

    for dr in resources.destination_rules:
        node_id = f"destinationrule:{dr.namespace}/{dr.name}"
        g.add_edge(node_id, f"namespace:{dr.namespace}", "in_namespace")
        _add_export_to_edges(
            g, node_id=node_id, namespace=dr.namespace, export_to=dr.export_to, namespaces=namespaces,
        )
        g.add_edge(node_id, f"host:{dr.host}", "configures_host")

    for gw in resources.gateways:
        node_id = f"gateway:{gw.namespace}/{gw.name}"
        g.add_edge(node_id, f"namespace:{gw.namespace}", "in_namespace")
        for server in gw.servers:
            for host in server.hosts:
                g.add_edge(
                    node_id, f"host:{host}", "exposes_host",
                    port=server.port_number, protocol=server.protocol, tls_mode=server.tls_mode,
                )
        _gateway_selector_edges(
            g, node_id=node_id, selector=gw.selector, pods=pods,
            workload_entries=resources.workload_entries,
        )

    for se in resources.service_entries:
        node_id = f"serviceentry:{se.namespace}/{se.name}"
        g.add_edge(node_id, f"namespace:{se.namespace}", "in_namespace")
        _add_export_to_edges(
            g, node_id=node_id, namespace=se.namespace, export_to=se.export_to, namespaces=namespaces,
        )
        for host in se.hosts:
            g.add_edge(node_id, f"host:{host}", "defines_host")
        _workload_scope_edges(
            g, node_id=node_id, namespace=se.namespace, selector=se.workload_selector, pods=pods,
            workload_entries=resources.workload_entries,
        )

    for sc in resources.sidecars:
        node_id = f"sidecar:{sc.namespace}/{sc.name}"
        g.add_edge(node_id, f"namespace:{sc.namespace}", "in_namespace")
        for host in sc.egress_hosts:
            g.add_edge(node_id, f"host:{host}", "egress_to")
        _workload_scope_edges(
            g, node_id=node_id, namespace=sc.namespace, selector=sc.workload_selector, pods=pods,
            workload_entries=resources.workload_entries,
            namespaces=namespaces, mesh_root_namespace=mesh_root_namespace,
        )

    for we in resources.workload_entries:
        node_id = f"workloadentry:{we.namespace}/{we.name}"
        g.add_edge(node_id, f"namespace:{we.namespace}", "in_namespace")
        if we.service_account:
            g.add_edge(node_id, f"serviceaccount:{we.namespace}/{we.service_account}", "uses_service_account")

    for wg in resources.workload_groups:
        node_id = f"workloadgroup:{wg.namespace}/{wg.name}"
        g.add_edge(node_id, f"namespace:{wg.namespace}", "in_namespace")
        if wg.service_account:
            g.add_edge(node_id, f"serviceaccount:{wg.namespace}/{wg.service_account}", "uses_service_account")

    for pa in resources.peer_authentications:
        node_id = f"peerauthentication:{pa.namespace}/{pa.name}"
        g.add_edge(node_id, f"namespace:{pa.namespace}", "in_namespace")
        _workload_scope_edges(
            g, node_id=node_id, namespace=pa.namespace, selector=pa.selector, pods=pods,
            workload_entries=resources.workload_entries,
            namespaces=namespaces, mesh_root_namespace=mesh_root_namespace,
        )

    for ap in resources.authorization_policies:
        node_id = f"authorizationpolicy:{ap.namespace}/{ap.name}"
        g.add_edge(node_id, f"namespace:{ap.namespace}", "in_namespace")
        if ap.target_refs:
            _add_target_ref_edges(g, node_id=node_id, namespace=ap.namespace, target_refs=ap.target_refs)
        else:
            _workload_scope_edges(
                g, node_id=node_id, namespace=ap.namespace, selector=ap.selector, pods=pods,
                workload_entries=resources.workload_entries,
                namespaces=namespaces, mesh_root_namespace=mesh_root_namespace,
            )
        for rule in ap.rules:
            for host in rule.to_hosts:
                g.add_edge(node_id, f"host:{host}", "controls_access_to", action=ap.action)
            for from_namespace in rule.from_namespaces:
                g.add_edge(
                    node_id, f"namespace:{from_namespace}", "allows_from_namespace", action=ap.action,
                )
            for principal in rule.from_principals:
                parsed = _parse_principal(principal)
                if parsed is None:
                    continue
                sa_namespace, sa_name = parsed
                g.add_edge(
                    node_id, f"serviceaccount:{sa_namespace}/{sa_name}", "allows_from", action=ap.action,
                )

    for ra in resources.request_authentications:
        node_id = f"requestauthentication:{ra.namespace}/{ra.name}"
        g.add_edge(node_id, f"namespace:{ra.namespace}", "in_namespace")
        if ra.target_refs:
            _add_target_ref_edges(g, node_id=node_id, namespace=ra.namespace, target_refs=ra.target_refs)
        else:
            _workload_scope_edges(
                g, node_id=node_id, namespace=ra.namespace, selector=ra.selector, pods=pods,
                workload_entries=resources.workload_entries,
                namespaces=namespaces, mesh_root_namespace=mesh_root_namespace,
            )


def _network_policy_peer_targets(
    peer: NetworkPolicyPeer, *, policy_namespace: str, namespaces: list[NamespaceInfo], pods: list[PodInfo],
) -> list[str]:
    """Ein NetworkPolicyPeer selektiert Pods in Namespaces, die vom
    namespace_selector erfasst werden (oder – falls dieser fehlt – im
    eigenen Namespace der Policy), zusätzlich gefiltert durch pod_selector,
    sofern angegeben; ein ip_block wird stattdessen als eigener, opaker
    CIDR-Knoten abgebildet."""
    if peer.ip_block_cidr:
        return [f"cidr:{peer.ip_block_cidr}"]

    if peer.namespace_selector:
        target_namespaces = [ns.name for ns in namespaces if _selector_matches(peer.namespace_selector, ns.labels)]
    else:
        target_namespaces = [policy_namespace]

    if not peer.pod_selector:
        return [f"namespace:{ns}" for ns in target_namespaces]

    return [
        f"pod:{pod.namespace}/{pod.name}"
        for pod in pods
        if pod.namespace in target_namespaces and _selector_matches(peer.pod_selector, pod.labels)
    ]


def _add_network_policy_edges(
    g: GraphBuilder, *, network_policies: list[NetworkPolicyInfo],
    namespaces: list[NamespaceInfo], pods: list[PodInfo],
) -> None:
    # Bildet für jede NetworkPolicy den eigenen Pod-Scope sowie die ingress-/
    # egress-Regeln auf Kanten ab. Pro Peer wird die Kantenrichtung bewusst
    # unterschiedlich gesetzt (Peer -> Policy bei ingress, Policy -> Peer bei
    # egress), damit der Graph tatsächlichen Traffic-Fluss abbildet statt nur
    # der Policy-Zugehörigkeit; ip_block-Peers werden dabei als eigene CIDR-
    # Knoten nachträglich ergänzt, da sie nicht Teil der vorab erzeugten Knoten
    # aus _add_core_nodes sind.
    for np in network_policies:
        node_id = f"networkpolicy:{np.namespace}/{np.name}"
        _workload_scope_edges(g, node_id=node_id, namespace=np.namespace, selector=np.pod_selector, pods=pods)

        for rule in np.ingress:
            ports = [dataclasses.asdict(p) for p in rule.ports]
            for peer in rule.peers:
                for target_id in _network_policy_peer_targets(
                    peer, policy_namespace=np.namespace, namespaces=namespaces, pods=pods,
                ):
                    g.add_node("cidr", peer.ip_block_cidr) if peer.ip_block_cidr else None
                    g.add_edge(target_id, node_id, "allows_ingress_to", ports=ports)

        for rule in np.egress:
            ports = [dataclasses.asdict(p) for p in rule.ports]
            for peer in rule.peers:
                for target_id in _network_policy_peer_targets(
                    peer, policy_namespace=np.namespace, namespaces=namespaces, pods=pods,
                ):
                    g.add_node("cidr", peer.ip_block_cidr) if peer.ip_block_cidr else None
                    g.add_edge(node_id, target_id, "allows_egress_to", ports=ports)


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def build_graph(namespace: str | None) -> dict[str, object]:
    mesh_root_namespace = get_mesh_root_namespace()
    namespaces = get_namespaces(namespace=namespace)
    services = get_services(namespace=namespace)
    service_accounts = get_service_accounts(namespace=namespace)
    pods = get_pods(namespace=namespace)
    network_policies = get_network_policies(namespace=namespace)
    resources = get_istio_resources(namespace=namespace)
    hosts = get_hosts(resources)

    g = GraphBuilder()
    # Reihenfolge: GraphBuilder.add_edge verwirft Kanten, deren
    # Quelle/Ziel noch nicht existiert (siehe add_edge oben), 
    # daher müssen zuerst alle Knoten definiert sein (Core- und Istio-Knoten), 
    # bevor überhaupt eine Kante gezogen wird.
    # _add_istio_nodes muss dabei vor _add_istio_edges laufen, und 
    # _add_network_policy_edges zuletzt, da es
    # sowohl auf Core- als auch auf Istio-Knoten (Namespaces, Pods) verweist.
    _add_core_nodes(
        g, namespaces=namespaces, services=services, service_accounts=service_accounts, pods=pods,
        network_policies=network_policies, hosts=hosts, mesh_root_namespace=mesh_root_namespace,
    )
    _add_service_pod_edges(g, services=services, pods=pods, workload_entries=resources.workload_entries)
    _add_host_service_edges(g, hosts=hosts, services=services)
    _add_istio_nodes(g, resources=resources)
    _add_istio_edges(
        g, resources=resources, pods=pods, namespaces=namespaces, mesh_root_namespace=mesh_root_namespace,
    )
    _add_network_policy_edges(g, network_policies=network_policies, namespaces=namespaces, pods=pods)

    return g.build()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Erstellt einen JSON-Abhängigkeitsgraphen (Knoten + Kanten) aus "
                    "allen erfassten Kubernetes-/Istio-Objekten und löst dabei "
                    "Label-Selektoren, Host-Strings, SPIFFE-Principals und targetRefs "
                    "zu expliziten Kanten auf.",
    )
    parser.add_argument(
        "-n", "--namespace",
        default=None,
        help="Erfasst nur Daten aus diesem Namespace (Standard: alle Namespaces im Cluster).",
    )
    parser.add_argument(
        "--insecure-skip-tls-verify",
        action="store_true",
        help="Deaktiviert die TLS-Zertifikatsprüfung gegenüber dem API-Server "
             "(entspricht kubectl/oc --insecure-skip-tls-verify).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Aktiviert Debug-Logging, z. B. für übersprungene/nicht auflösbare Kanten.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )

    try:
        load_config(verify_ssl=not args.insecure_skip_tls_verify)
    except ConfigException as e:
        print(f"Fehler: Kubernetes-Konfiguration konnte nicht geladen werden: {e}", file=sys.stderr)
        return 1

    try:
        graph = build_graph(args.namespace)
    except (ApiException, urllib3.exceptions.HTTPError) as e:
        print(f"Fehler: Der Kubernetes-API-Server konnte nicht erreicht werden: {e}", file=sys.stderr)
        return 1

    print(json.dumps(graph, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
