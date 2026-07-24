"""CLI: Baut aus denselben Kubernetes-/Istio-Objekten wie istio-graph.py einen
Deployment-zentrierten *Verbindungsgraphen*.

Während istio-graph.py jede aufgelöste Label-Selektor-/Host-/Principal-
Beziehung 1:1 als Kante abbildet, zeigt dieser Graph nur, welche
Deployment-zu-Deployment-Verbindungen dadurch tatsächlich möglich sind — über
die dafür nötigen Routing-Hops (Gateway, Service, ServiceEntry,
VirtualService) sowie explizit durch eine AuthorizationPolicy verbotene
Verbindungen.

Modell
------
- Jede Verbindung beginnt an einem Gateway oder einem Deployment und endet
  immer an einem Deployment; alle anderen Knotenarten (Service, ServiceEntry,
  VirtualService, AuthorizationPolicy) sind reine Zwischen-Hops — Kanten sind
  Teilstrecken einer solchen Verbindung. DestinationRules bleiben bewusst
  außen vor.
- "Möglich" heißt hier: technisch über Service-Selektoren, VirtualService-
  Routing oder Gateway-Exposition erreichbar — unabhängig davon, ob ein Pod
  diese Route tatsächlich nutzt (daher z. B. immer eine Kante von *jedem*
  Deployment zu einem direkt erreichbaren Service, nicht nur von den
  Deployments, die ihn im Betrieb tatsächlich aufrufen).
- AuthorizationPolicy(ALLOW) wird für diesen Graphen NICHT als Filter
  herangezogen: eine Menge erlaubter Aufrufer zu berechnen würde zwangsläufig
  alle dadurch implizit verbotenen Verbindungen sichtbar machen (z. B. über
  eine leere "default-deny" Policy) — das ist laut Vorgabe explizit
  unerwünscht.
- Nur AuthorizationPolicy(DENY)-Regeln mit tatsächlichem Inhalt (from/to
  nicht komplett leer) erzeugen zusätzliche Kanten mit relation="forbidden":
  von der aufgelösten Quelle über die Policy zum betroffenen Deployment. Eine
  komplett leere Regel (das "default-deny"-Muster: eine Policy ohne jede
  Einschränkung) wird ignoriert und erzeugt keine Kante.
- ServiceAccounts sind kein eigener Knoten mehr, sondern ein Attribut
  (``service_account``) direkt am jeweiligen Deployment-Knoten.

Bekannte Vereinfachungen (bewusst, um den Graphen beherrschbar zu halten):
NetworkPolicies, Sidecars, PeerAuthentication/RequestAuthentication und
``exportTo``-Namespace-Beschränkungen fließen nicht ein — sie beeinflussen
zwar die tatsächliche Erreichbarkeit, gehören aber nicht zu den in der
Vorgabe genannten sechs Knotenarten. Gateway<->VirtualService wird wie in
istio-graph.py rein über den Namen in ``spec.gateways`` aufgelöst, ohne
zusätzlichen Abgleich der exponierten Hosts.

Eine wichtige Designentscheidung:
ALLOW-AuthorizationPolicies fließen gar nicht in den Graphen ein, weder als Filter noch als Knoten. 
Grund: 
Eine Menge erlaubter Aufrufer zu berechnen würde zwangsläufig alle impliziten Verbote sichtbar machen 
(genau das, was laut Vorgabe nicht gezeigt werden soll). 
Falls doch explizite ALLOW-Listen die "möglich"-Kanten einschränken (nur eben ohne die implizite Restmenge zu zeigen), 
lässt sich das nachrüsten, ist aber pro Ziel-Deployment nicht immer eindeutig graphdarstellbar, 
wenn ein Service mehrere Deployments mit unterschiedlichen Policies bedient.
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

from istio import (
    AuthorizationPolicyInfo,
    AuthorizationRule,
    ServiceEntryInfo,
    VirtualServiceInfo,
    get_istio_resources,
)
from kubectl import (
    DeploymentInfo,
    ServiceInfo,
    get_deployments,
    get_mesh_root_namespace,
    get_services,
    load_config,
)

logger = logging.getLogger(__name__)

# SPIFFE-Principal, z. B. "cluster.local/ns/default/sa/httpbin".
_PRINCIPAL_RE = re.compile(r"/ns/(?P<namespace>[^/]+)/sa/(?P<name>[^/]+)$")


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
    """Sammelt Knoten und Kanten und entfernt beim Bauen alles, was nicht Teil
    einer vollständigen, an einem Deployment endenden Verbindung ist."""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: dict[tuple[str, str, str], Edge] = {}

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
        # damit ein nicht auflösbarer Host keine hängende Kante erzeugt.
        if source not in self._nodes or target not in self._nodes:
            logger.debug("Kante %s -%s-> %s wird übersprungen: Endpunkt nicht gefunden", source, relation, target)
            return
        key = (source, target, relation)
        if key not in self._edges:
            self._edges[key] = Edge(source=source, target=target, relation=relation, attributes=attributes)

    def build(self) -> dict[str, object]:
        # Rückwärts-Fixpunkt: ein Knoten "kann ein Deployment erreichen", wenn
        # er selbst eines ist, oder eine Kante zu einem Knoten hat, der eines
        # erreichen kann. Nur Kanten, deren Ziel in dieser Menge liegt, gehören
        # zu einer vollständigen Verbindung und werden behalten — das setzt
        # "Verbindungen enden immer an einem Deployment" strukturell durch,
        # statt es an jeder Konstruktionsstelle einzeln sicherzustellen.
        deployment_ids = {n.id for n in self._nodes.values() if n.kind == "deployment"}
        can_reach_deployment = set(deployment_ids)
        changed = True
        while changed:
            changed = False
            for edge in self._edges.values():
                if edge.target in can_reach_deployment and edge.source not in can_reach_deployment:
                    can_reach_deployment.add(edge.source)
                    changed = True

        kept_edges = [e for e in self._edges.values() if e.target in can_reach_deployment]
        # Deployment-Knoten bleiben immer erhalten (auch ohne Kanten), alle
        # anderen Knotenarten nur, wenn sie tatsächlich Teil einer Verbindung sind.
        kept_node_ids = set(deployment_ids)
        for e in kept_edges:
            kept_node_ids.add(e.source)
            kept_node_ids.add(e.target)

        nodes = sorted((n for n in self._nodes.values() if n.id in kept_node_ids), key=lambda n: n.id)
        edges = sorted(kept_edges, key=lambda e: (e.source, e.target, e.relation))
        return {
            "nodes": [dataclasses.asdict(n) for n in nodes],
            "edges": [dataclasses.asdict(e) for e in edges],
        }


# ---------------------------------------------------------------------------
# Hilfsfunktionen zur Auflösung von Selektoren/Hosts/Principals
# ---------------------------------------------------------------------------

def _selector_matches(selector: dict[str, str], labels: dict[str, str]) -> bool:
    if not selector:
        return False
    return all(labels.get(k) == v for k, v in selector.items())


def _host_matches_service(host: str, svc: ServiceInfo) -> bool:
    """Gleicht einen Host-String aus VirtualService/Gateway/... gegen den
    Cluster-DNS-Namen eines Service ab, in jeder seiner Kurz-/FQDN-Formen."""
    if not host or host == "*":
        return False
    short = f"{svc.name}.{svc.namespace}"
    return host == svc.name or host == short or host.startswith(f"{short}.")


def _host_matches_pattern(host: str, pattern: str) -> bool:
    """Gleicht einen konkreten Host-String gegen ein ServiceEntry/Gateway-
    Host-Pattern ab, das ein führendes Wildcard-Label enthalten darf
    (``*.example.com``) oder als reines ``*`` alles matcht."""
    if not host or not pattern:
        return False
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        suffix = pattern[1:]
        return host != suffix.lstrip(".") and host.endswith(suffix)
    return host == pattern


def _hosts_overlap(a: str, b: str) -> bool:
    return _host_matches_pattern(a, b) or _host_matches_pattern(b, a)


def _parse_principal(principal: str) -> tuple[str, str] | None:
    m = _PRINCIPAL_RE.search(principal)
    return (m.group("namespace"), m.group("name")) if m else None


def _vs_gateway_refs(vs: VirtualServiceInfo) -> tuple[bool, list[tuple[str, str]]]:
    """Liefert, ob ein VirtualService mesh-weit (Sidecar-seitig, für
    ausgehende Aufrufe *jedes* Deployments) gilt — Default, oder wenn
    ``gateways`` explizit "mesh" enthält — sowie die (Namespace, Name)-Paare
    aller referenzierten Gateways."""
    mesh_wide = not vs.gateways or "mesh" in vs.gateways
    refs = []
    for gw in vs.gateways:
        if gw == "mesh":
            continue
        gw_namespace, _, gw_name = gw.rpartition("/")
        refs.append((gw_namespace or vs.namespace, gw_name))
    return mesh_wide, refs


def _service_backing_deployments(svc: ServiceInfo, deployments: list[DeploymentInfo]) -> list[DeploymentInfo]:
    return [d for d in deployments if d.namespace == svc.namespace and _selector_matches(svc.selector, d.labels)]


def _service_entry_backing_deployments(
    se: ServiceEntryInfo, deployments: list[DeploymentInfo],
) -> list[DeploymentInfo]:
    # Nur ServiceEntries mit workloadSelector repräsentieren Mesh-interne
    # Deployments (z. B. für Multi-Network-Setups) — reine externe Endpunkte
    # haben keinen zugehörigen Pod und können die Vorgabe "Verbindungen enden
    # immer an einem Deployment" nicht erfüllen.
    if not se.workload_selector:
        return []
    return [
        d for d in deployments
        if d.namespace == se.namespace and _selector_matches(se.workload_selector, d.labels)
    ]


# ---------------------------------------------------------------------------
# AuthorizationPolicy(DENY) -> "forbidden"-Kanten
# ---------------------------------------------------------------------------

def _authz_policy_targets(
    ap: AuthorizationPolicyInfo, *, deployments: list[DeploymentInfo], services: list[ServiceInfo],
    mesh_root_namespace: str,
) -> list[DeploymentInfo]:
    """Löst auf, welche Deployments eine AuthorizationPolicy betrifft — über
    targetRefs (nur Kind "Service", auf die zugehörigen Deployments
    aufgelöst), einen Label-Selector, oder, wenn beides fehlt, den gesamten
    Namespace (mesh-weit, falls die Policy im Root-Namespace liegt)."""
    if ap.target_refs:
        targets: dict[str, DeploymentInfo] = {}
        for ref in ap.target_refs:
            if ref.kind != "Service":
                continue
            for svc in services:
                if svc.namespace == ap.namespace and svc.name == ref.name:
                    for d in _service_backing_deployments(svc, deployments):
                        targets[f"{d.namespace}/{d.name}"] = d
        return list(targets.values())
    if ap.selector:
        return [d for d in deployments if d.namespace == ap.namespace and _selector_matches(ap.selector, d.labels)]
    if ap.namespace == mesh_root_namespace:
        return list(deployments)
    return [d for d in deployments if d.namespace == ap.namespace]


def _resolve_rule_sources(rule: AuthorizationRule, deployments: list[DeploymentInfo]) -> list[DeploymentInfo]:
    """Löst die Quell-Deployments einer AuthorizationRule auf. Ohne
    ``from``-Einschränkung (aber mit z. B. einem ``to.hosts``) gilt die Regel
    explizit für jede Quelle — das ist nicht das "default-deny"-Muster (eine
    komplett leere Regel), sondern eine bewusst weit gefasste, aber konkrete
    Sperre."""
    if not rule.from_namespaces and not rule.from_principals:
        return list(deployments)
    sources: dict[str, DeploymentInfo] = {}
    for d in deployments:
        if d.namespace in rule.from_namespaces:
            sources[f"{d.namespace}/{d.name}"] = d
    for principal in rule.from_principals:
        parsed = _parse_principal(principal)
        if parsed is None:
            continue
        sa_namespace, sa_name = parsed
        for d in deployments:
            if d.namespace == sa_namespace and d.service_account == sa_name:
                sources[f"{d.namespace}/{d.name}"] = d
    return list(sources.values())


def _add_forbidden_edges(
    g: GraphBuilder, *, authorization_policies: list[AuthorizationPolicyInfo],
    deployments: list[DeploymentInfo], services: list[ServiceInfo], mesh_root_namespace: str,
) -> None:
    for ap in authorization_policies:
        if ap.action != "DENY":
            continue
        # Eine Regel ohne jede Einschränkung (weder from noch to) ist das
        # "default-deny"-Muster — implizite Verbote sollen laut Vorgabe nicht
        # gezeigt werden, also werden solche Regeln übersprungen. Eine Policy
        # ganz ohne Regeln fällt automatisch mit heraus.
        meaningful_rules = [r for r in ap.rules if r.from_namespaces or r.from_principals or r.to_hosts]
        if not meaningful_rules:
            continue
        targets = _authz_policy_targets(
            ap, deployments=deployments, services=services, mesh_root_namespace=mesh_root_namespace,
        )
        if not targets:
            continue
        ap_id = g.add_node("authorizationpolicy", ap.name, ap.namespace, action=ap.action)
        for rule in meaningful_rules:
            sources = _resolve_rule_sources(rule, deployments)
            for target in targets:
                target_id = f"deployment:{target.namespace}/{target.name}"
                for source in sources:
                    source_id = f"deployment:{source.namespace}/{source.name}"
                    g.add_edge(source_id, ap_id, "forbidden")
                    g.add_edge(ap_id, target_id, "forbidden")


# ---------------------------------------------------------------------------
# "Mögliche" Verbindungen: Gateway/Deployment -> ... -> Deployment
# ---------------------------------------------------------------------------

def _add_direct_service_edges(
    g: GraphBuilder, *, services: list[ServiceInfo], deployments: list[DeploymentInfo],
    gateways_by_host: list[tuple[str, str]], vs_hosts: list[str],
) -> None:
    """Direkter Aufruf eines Service über seinen Cluster-DNS-Namen, ohne
    dazwischenliegenden VirtualService — der Default in Istio, solange kein
    VirtualService für den Host existiert (existiert einer, übernimmt der
    dessen Routing vollständig, siehe _add_virtual_service_edges)."""
    for svc in services:
        if any(_host_matches_service(h, svc) for h in vs_hosts):
            continue
        backing = _service_backing_deployments(svc, deployments)
        if not backing:
            continue
        svc_id = f"service:{svc.namespace}/{svc.name}"
        for dep in deployments:
            g.add_edge(f"deployment:{dep.namespace}/{dep.name}", svc_id, "may_call")
        for dep in backing:
            g.add_edge(svc_id, f"deployment:{dep.namespace}/{dep.name}", "selects")
        for gw_id, host in gateways_by_host:
            if _host_matches_service(host, svc):
                g.add_edge(gw_id, svc_id, "exposes")


def _add_direct_service_entry_edges(
    g: GraphBuilder, *, service_entries: list[ServiceEntryInfo], deployments: list[DeploymentInfo],
    vs_hosts: list[str],
) -> None:
    """Direkter Aufruf einer ServiceEntry (Mesh-interner Alias, siehe
    _service_entry_backing_deployments) ohne dazwischenliegenden
    VirtualService."""
    for se in service_entries:
        if any(_hosts_overlap(h, vh) for h in se.hosts for vh in vs_hosts):
            continue
        backing = _service_entry_backing_deployments(se, deployments)
        if not backing:
            continue
        se_id = f"serviceentry:{se.namespace}/{se.name}"
        for dep in deployments:
            g.add_edge(f"deployment:{dep.namespace}/{dep.name}", se_id, "may_call")
        for dep in backing:
            g.add_edge(se_id, f"deployment:{dep.namespace}/{dep.name}", "resolves_to")


def _add_virtual_service_edges(
    g: GraphBuilder, *, virtual_services: list[VirtualServiceInfo], services: list[ServiceInfo],
    service_entries: list[ServiceEntryInfo], deployments: list[DeploymentInfo],
) -> None:
    for vs in virtual_services:
        vs_id = f"virtualservice:{vs.namespace}/{vs.name}"
        mesh_wide, gw_refs = _vs_gateway_refs(vs)
        if mesh_wide:
            for dep in deployments:
                g.add_edge(f"deployment:{dep.namespace}/{dep.name}", vs_id, "routes_via")
        for gw_namespace, gw_name in gw_refs:
            g.add_edge(f"gateway:{gw_namespace}/{gw_name}", vs_id, "exposes")

        for dest in vs.destinations:
            for svc in services:
                if not _host_matches_service(dest.host, svc):
                    continue
                svc_id = f"service:{svc.namespace}/{svc.name}"
                g.add_edge(vs_id, svc_id, "routes_to")
                for dep in _service_backing_deployments(svc, deployments):
                    g.add_edge(svc_id, f"deployment:{dep.namespace}/{dep.name}", "selects")
            for se in service_entries:
                if not any(_hosts_overlap(dest.host, h) for h in se.hosts):
                    continue
                se_id = f"serviceentry:{se.namespace}/{se.name}"
                g.add_edge(vs_id, se_id, "routes_to")
                for dep in _service_entry_backing_deployments(se, deployments):
                    g.add_edge(se_id, f"deployment:{dep.namespace}/{dep.name}", "resolves_to")


# ---------------------------------------------------------------------------
# Einstiegspunkt
# ---------------------------------------------------------------------------

def build_graph(namespace: str | None) -> dict[str, object]:
    mesh_root_namespace = get_mesh_root_namespace()
    deployments = get_deployments(namespace=namespace)
    services = get_services(namespace=namespace)
    resources = get_istio_resources(namespace=namespace)

    g = GraphBuilder()
    for dep in deployments:
        g.add_node("deployment", dep.name, dep.namespace, service_account=dep.service_account, labels=dep.labels)
    for gw in resources.gateways:
        g.add_node("gateway", gw.name, gw.namespace, direction=gw.direction)
    for svc in services:
        g.add_node("service", svc.name, svc.namespace, ports=svc.ports)
    for se in resources.service_entries:
        g.add_node("serviceentry", se.name, se.namespace, hosts=se.hosts, location=se.location)
    for vs in resources.virtual_services:
        g.add_node("virtualservice", vs.name, vs.namespace, hosts=vs.hosts)

    # Hosts, die von mindestens einem VirtualService abgedeckt werden — für
    # solche Hosts übernimmt dessen Routing vollständig, ein direkter
    # Service-/ServiceEntry-Aufruf (an den Envoy-Sidecars vorbei) ist dann
    # nicht mehr der reale Pfad.
    vs_hosts = [h for vs in resources.virtual_services for h in vs.hosts]
    gateways_by_host = [
        (f"gateway:{gw.namespace}/{gw.name}", host)
        for gw in resources.gateways
        for server in gw.servers
        for host in server.hosts
    ]

    _add_direct_service_edges(
        g, services=services, deployments=deployments, gateways_by_host=gateways_by_host, vs_hosts=vs_hosts,
    )
    _add_direct_service_entry_edges(
        g, service_entries=resources.service_entries, deployments=deployments, vs_hosts=vs_hosts,
    )
    _add_virtual_service_edges(
        g, virtual_services=resources.virtual_services, services=services,
        service_entries=resources.service_entries, deployments=deployments,
    )
    _add_forbidden_edges(
        g, authorization_policies=resources.authorization_policies, deployments=deployments,
        services=services, mesh_root_namespace=mesh_root_namespace,
    )

    return g.build()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Erstellt einen Deployment-zentrierten JSON-Verbindungsgraphen: welche "
                    "Deployment-zu-Deployment-Verbindungen sind über Gateway/Service/ServiceEntry/"
                    "VirtualService möglich, und welche sind explizit per AuthorizationPolicy(DENY) "
                    "verboten (relation=\"forbidden\").",
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
