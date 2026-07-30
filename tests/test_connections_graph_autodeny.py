from __future__ import annotations

import pytest

import kubectl
from istio import (
    AuthorizationPolicyInfo,
    AuthorizationRule,
    GatewayInfo,
    GatewayServer,
    IstioResources,
    RouteDestination,
    ServiceEntryInfo,
    TargetRef,
    VirtualServiceInfo,
)


@pytest.fixture
def cg(load_module):
    return load_module("connections_graph_autodeny", "connections-graph-autodeny.py")


# ---------------------------------------------------------------------------
# Selector-/Host-/Principal-Hilfsfunktionen
# ---------------------------------------------------------------------------

def test_selector_matches_empty_selector_never_matches(cg):
    assert cg._selector_matches({}, {"app": "foo"}) is False


def test_selector_matches_all_keys_present(cg):
    assert cg._selector_matches({"app": "foo"}, {"app": "foo", "version": "v1"}) is True


@pytest.mark.parametrize("host,expected", [
    ("httpbin", True),
    ("httpbin.default", True),
    ("httpbin.default.svc.cluster.local", True),
    ("other-svc", False),
    ("*", False),
    ("", False),
])
def test_host_matches_service(cg, host, expected):
    svc = kubectl.ServiceInfo(name="httpbin", namespace="default")
    assert cg._host_matches_service(host, svc) is expected


@pytest.mark.parametrize("host,pattern,expected", [
    ("api.example.com", "*", True),
    ("api.example.com", "*.example.com", True),
    ("example.com", "*.example.com", False),
    ("api.example.com", "api.example.com", True),
    ("api.example.com", "other.example.com", False),
])
def test_host_matches_pattern(cg, host, pattern, expected):
    assert cg._host_matches_pattern(host, pattern) is expected


def test_hosts_overlap_is_symmetric(cg):
    assert cg._hosts_overlap("api.example.com", "*.example.com") is True
    assert cg._hosts_overlap("*.example.com", "api.example.com") is True
    assert cg._hosts_overlap("api.example.com", "other.example.com") is False


def test_parse_principal_valid(cg):
    assert cg._parse_principal("cluster.local/ns/default/sa/sleep") == ("default", "sleep")


def test_parse_principal_invalid(cg):
    assert cg._parse_principal("not-a-principal") is None


def test_vs_gateway_refs_default_is_mesh_wide(cg):
    vs = VirtualServiceInfo(name="reviews", namespace="default")
    mesh_wide, refs = cg._vs_gateway_refs(vs)
    assert mesh_wide is True
    assert refs == []


def test_vs_gateway_refs_explicit_gateway_only(cg):
    vs = VirtualServiceInfo(name="reviews", namespace="default", gateways=["istio-system/ingress"])
    mesh_wide, refs = cg._vs_gateway_refs(vs)
    assert mesh_wide is False
    assert refs == [("istio-system", "ingress")]


def test_vs_gateway_refs_mesh_and_gateway_combined(cg):
    vs = VirtualServiceInfo(name="reviews", namespace="default", gateways=["mesh", "ingress"])
    mesh_wide, refs = cg._vs_gateway_refs(vs)
    assert mesh_wide is True
    assert refs == [("default", "ingress")]


# ---------------------------------------------------------------------------
# GraphBuilder
# ---------------------------------------------------------------------------

def test_graph_builder_add_node_dedups_by_id(cg):
    g = cg.GraphBuilder()
    id1 = g.add_node("service", "httpbin", "default", ports=[80])
    id2 = g.add_node("service", "httpbin", "default", ports=[9080])
    assert id1 == id2


def test_graph_builder_add_edge_dropped_when_endpoint_missing(cg):
    g = cg.GraphBuilder()
    g.add_node("deployment", "a", "default")
    g.add_edge("deployment:default/a", "deployment:default/missing", "may_call")
    assert g.build()["edges"] == []


def test_graph_builder_add_edge_dedups_identical_edges(cg):
    g = cg.GraphBuilder()
    g.add_node("deployment", "a", "default")
    g.add_node("deployment", "b", "default")
    g.add_edge("deployment:default/a", "deployment:default/b", "may_call")
    g.add_edge("deployment:default/a", "deployment:default/b", "may_call")
    assert len(g.build()["edges"]) == 1


def test_graph_builder_build_keeps_deployments_with_no_edges(cg):
    g = cg.GraphBuilder()
    g.add_node("deployment", "lonely", "default")
    graph = g.build()
    assert [n["id"] for n in graph["nodes"]] == ["deployment:default/lonely"]


def test_graph_builder_build_prunes_hops_that_never_reach_a_deployment(cg):
    g = cg.GraphBuilder()
    g.add_node("deployment", "caller", "default")
    g.add_node("service", "dead-end", "default")
    g.add_edge("deployment:default/caller", "service:default/dead-end", "may_call")
    graph = g.build()
    assert graph["edges"] == []
    assert [n["id"] for n in graph["nodes"]] == ["deployment:default/caller"]


def test_graph_builder_build_keeps_full_chain_ending_in_deployment(cg):
    g = cg.GraphBuilder()
    g.add_node("deployment", "caller", "default")
    g.add_node("service", "svc", "default")
    g.add_node("deployment", "target", "default")
    g.add_edge("deployment:default/caller", "service:default/svc", "may_call")
    g.add_edge("service:default/svc", "deployment:default/target", "selects")
    graph = g.build()
    node_ids = {n["id"] for n in graph["nodes"]}
    assert node_ids == {"deployment:default/caller", "service:default/svc", "deployment:default/target"}
    assert len(graph["edges"]) == 2


# ---------------------------------------------------------------------------
# Backing-Deployment-Auflösung
# ---------------------------------------------------------------------------

def test_service_backing_deployments_matches_namespace_and_selector(cg):
    deployments = [
        kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"}),
        kubectl.DeploymentInfo(name="other", namespace="default", labels={"app": "other"}),
        kubectl.DeploymentInfo(name="httpbin", namespace="ns2", labels={"app": "httpbin"}),
    ]
    svc = kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})
    result = cg._service_backing_deployments(svc, deployments)
    assert result == [deployments[0]]


def test_service_entry_backing_deployments_requires_workload_selector(cg):
    deployments = [kubectl.DeploymentInfo(name="vm-proxy", namespace="default", labels={"app": "vm-proxy"})]
    se_external = ServiceEntryInfo(name="external", namespace="default", hosts=["api.example.com"])
    se_internal = ServiceEntryInfo(
        name="internal", namespace="default", hosts=["vm-proxy.default.svc.cluster.local"],
        workload_selector={"app": "vm-proxy"},
    )
    assert cg._service_entry_backing_deployments(se_external, deployments) == []
    assert cg._service_entry_backing_deployments(se_internal, deployments) == deployments


# ---------------------------------------------------------------------------
# AuthorizationPolicy-Auflösung (Quellen/Ziele)
# ---------------------------------------------------------------------------

def test_resolve_rule_sources_empty_from_means_any_source(cg):
    deployments = [kubectl.DeploymentInfo(name="a", namespace="default", labels={})]
    rule = AuthorizationRule(to_hosts=["httpbin.default"])
    assert cg._resolve_rule_sources(rule, deployments) == deployments


def test_resolve_rule_sources_by_namespace(cg):
    deployments = [
        kubectl.DeploymentInfo(name="a", namespace="ns1", labels={}),
        kubectl.DeploymentInfo(name="b", namespace="ns2", labels={}),
    ]
    rule = AuthorizationRule(from_namespaces=["ns1"])
    assert cg._resolve_rule_sources(rule, deployments) == [deployments[0]]


def test_resolve_rule_sources_by_principal(cg):
    deployments = [
        kubectl.DeploymentInfo(name="a", namespace="default", labels={}, service_account="sleep"),
        kubectl.DeploymentInfo(name="b", namespace="default", labels={}, service_account="other"),
    ]
    rule = AuthorizationRule(from_principals=["cluster.local/ns/default/sa/sleep"])
    assert cg._resolve_rule_sources(rule, deployments) == [deployments[0]]


def test_authz_policy_targets_by_selector(cg):
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    ap = AuthorizationPolicyInfo(
        name="deny-httpbin", namespace="default", action="DENY", selector={"app": "httpbin"},
    )
    result = cg._authz_policy_targets(
        ap, deployments=deployments, services=[], mesh_root_namespace="istio-system",
    )
    assert result == deployments


def test_authz_policy_targets_by_target_ref_service(cg):
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})]
    ap = AuthorizationPolicyInfo(
        name="deny-httpbin", namespace="default", action="DENY",
        target_refs=[TargetRef(kind="Service", name="httpbin")],
    )
    result = cg._authz_policy_targets(
        ap, deployments=deployments, services=services, mesh_root_namespace="istio-system",
    )
    assert result == deployments


def test_authz_policy_targets_empty_selector_applies_to_own_namespace(cg):
    deployments = [
        kubectl.DeploymentInfo(name="a", namespace="default", labels={}),
        kubectl.DeploymentInfo(name="b", namespace="other", labels={}),
    ]
    ap = AuthorizationPolicyInfo(name="deny-all", namespace="default", action="DENY")
    result = cg._authz_policy_targets(
        ap, deployments=deployments, services=[], mesh_root_namespace="istio-system",
    )
    assert result == [deployments[0]]


def test_authz_policy_targets_empty_selector_mesh_root_applies_mesh_wide(cg):
    deployments = [
        kubectl.DeploymentInfo(name="a", namespace="default", labels={}),
        kubectl.DeploymentInfo(name="b", namespace="other", labels={}),
    ]
    ap = AuthorizationPolicyInfo(name="deny-all", namespace="istio-system", action="DENY")
    result = cg._authz_policy_targets(
        ap, deployments=deployments, services=[], mesh_root_namespace="istio-system",
    )
    assert result == deployments


# ---------------------------------------------------------------------------
# AuthorizationPolicy(DENY) -> forbidden
# ---------------------------------------------------------------------------

def test_add_forbidden_edges_skips_default_deny_pattern(cg):
    g = cg.GraphBuilder()
    deployments = [
        kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"}),
        kubectl.DeploymentInfo(name="sleep", namespace="default", labels={"app": "sleep"}),
    ]
    for d in deployments:
        g.add_node("deployment", d.name, d.namespace)
    ap = AuthorizationPolicyInfo(
        name="default-deny", namespace="default", action="DENY",
        selector={"app": "httpbin"}, rules=[AuthorizationRule()],
    )
    cg._add_forbidden_edges(
        g, authorization_policies=[ap], deployments=deployments, services=[], mesh_root_namespace="istio-system",
    )
    assert g.build()["edges"] == []


def test_add_forbidden_edges_explicit_deny_creates_labeled_chain(cg):
    g = cg.GraphBuilder()
    deployments = [
        kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"}),
        kubectl.DeploymentInfo(name="sleep", namespace="default", labels={"app": "sleep"}, service_account="sleep"),
    ]
    for d in deployments:
        g.add_node("deployment", d.name, d.namespace)
    ap = AuthorizationPolicyInfo(
        name="deny-sleep", namespace="default", action="DENY", selector={"app": "httpbin"},
        rules=[AuthorizationRule(from_principals=["cluster.local/ns/default/sa/sleep"])],
    )
    cg._add_forbidden_edges(
        g, authorization_policies=[ap], deployments=deployments, services=[], mesh_root_namespace="istio-system",
    )
    graph = g.build()
    relations = {(e["source"], e["target"], e["relation"]) for e in graph["edges"]}
    assert relations == {
        ("deployment:default/sleep", "authorizationpolicy:default/deny-sleep", "forbidden"),
        ("authorizationpolicy:default/deny-sleep", "deployment:default/httpbin", "forbidden"),
    }


def test_add_forbidden_edges_ignores_allow_policies(cg):
    g = cg.GraphBuilder()
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    g.add_node("deployment", "httpbin", "default")
    ap = AuthorizationPolicyInfo(
        name="allow-httpbin", namespace="default", action="ALLOW", selector={"app": "httpbin"},
        rules=[AuthorizationRule(from_namespaces=["default"])],
    )
    cg._add_forbidden_edges(
        g, authorization_policies=[ap], deployments=deployments, services=[], mesh_root_namespace="istio-system",
    )
    assert g.build()["edges"] == []


# ---------------------------------------------------------------------------
# AuthorizationPolicy(ALLOW) -> Kanten-Label
# ---------------------------------------------------------------------------

def test_add_allow_policy_labels_annotates_existing_selects_edge(cg):
    g = cg.GraphBuilder()
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})]
    g.add_node("deployment", "httpbin", "default")
    g.add_node("service", "httpbin", "default")
    g.add_edge("service:default/httpbin", "deployment:default/httpbin", "selects")
    ap = AuthorizationPolicyInfo(
        name="allow-sleep", namespace="default", action="ALLOW", selector={"app": "httpbin"},
        rules=[AuthorizationRule(from_namespaces=["default"])],
    )
    cg._add_allow_policy_labels(
        g, authorization_policies=[ap], deployments=deployments, services=services,
        mesh_root_namespace="istio-system",
    )
    graph = g.build()
    edge = next(
        e for e in graph["edges"]
        if e["source"] == "service:default/httpbin" and e["target"] == "deployment:default/httpbin"
    )
    assert edge["attributes"]["allow_policies"] == [{
        "name": "allow-sleep",
        "namespace": "default",
        "rules": [{"from_namespaces": ["default"], "from_principals": [], "to_hosts": []}],
    }]


def test_add_allow_policy_labels_skips_default_deny_style_empty_rule(cg):
    g = cg.GraphBuilder()
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})]
    g.add_node("deployment", "httpbin", "default")
    g.add_node("service", "httpbin", "default")
    g.add_edge("service:default/httpbin", "deployment:default/httpbin", "selects")
    ap = AuthorizationPolicyInfo(
        name="allow-all", namespace="default", action="ALLOW",
        selector={"app": "httpbin"}, rules=[AuthorizationRule()],
    )
    cg._add_allow_policy_labels(
        g, authorization_policies=[ap], deployments=deployments, services=services,
        mesh_root_namespace="istio-system",
    )
    graph = g.build()
    edge = next(
        e for e in graph["edges"]
        if e["source"] == "service:default/httpbin" and e["target"] == "deployment:default/httpbin"
    )
    assert "allow_policies" not in edge["attributes"]


def test_add_allow_policy_labels_ignores_deny_policies(cg):
    g = cg.GraphBuilder()
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})]
    g.add_node("deployment", "httpbin", "default")
    g.add_node("service", "httpbin", "default")
    g.add_edge("service:default/httpbin", "deployment:default/httpbin", "selects")
    ap = AuthorizationPolicyInfo(
        name="deny-sleep", namespace="default", action="DENY", selector={"app": "httpbin"},
        rules=[AuthorizationRule(from_namespaces=["default"])],
    )
    cg._add_allow_policy_labels(
        g, authorization_policies=[ap], deployments=deployments, services=services,
        mesh_root_namespace="istio-system",
    )
    graph = g.build()
    edge = next(
        e for e in graph["edges"]
        if e["source"] == "service:default/httpbin" and e["target"] == "deployment:default/httpbin"
    )
    assert "allow_policies" not in edge["attributes"]


# ---------------------------------------------------------------------------
# AuthorizationPolicy(DENY) -> Kanten-Label (deny_policies)
# ---------------------------------------------------------------------------

def test_add_deny_policy_labels_annotates_existing_selects_edge(cg):
    g = cg.GraphBuilder()
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})]
    g.add_node("deployment", "httpbin", "default")
    g.add_node("service", "httpbin", "default")
    g.add_edge("service:default/httpbin", "deployment:default/httpbin", "selects")
    ap = AuthorizationPolicyInfo(
        name="deny-sleep", namespace="default", action="DENY", selector={"app": "httpbin"},
        rules=[AuthorizationRule(from_principals=["cluster.local/ns/default/sa/sleep"])],
    )
    cg._add_deny_policy_labels(
        g, authorization_policies=[ap], deployments=deployments, services=services,
        mesh_root_namespace="istio-system",
    )
    graph = g.build()
    edge = next(
        e for e in graph["edges"]
        if e["source"] == "service:default/httpbin" and e["target"] == "deployment:default/httpbin"
    )
    assert edge["attributes"]["deny_policies"] == [{
        "name": "deny-sleep",
        "namespace": "default",
        "action": "DENY",
        "implicit_deny_all": False,
        "rules": [{"from_namespaces": [], "from_principals": ["cluster.local/ns/default/sa/sleep"], "to_hosts": []}],
    }]


def test_add_deny_policy_labels_does_not_skip_default_deny_style_empty_rule(cg):
    # Anders als _add_forbidden_edges: die leere "default-deny"-Regel soll
    # hier NICHT ignoriert werden, damit sichtbar bleibt, dass das Deployment
    # überhaupt unter einer DENY-Policy steht.
    g = cg.GraphBuilder()
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})]
    g.add_node("deployment", "httpbin", "default")
    g.add_node("service", "httpbin", "default")
    g.add_edge("service:default/httpbin", "deployment:default/httpbin", "selects")
    ap = AuthorizationPolicyInfo(
        name="default-deny", namespace="default", action="DENY",
        selector={"app": "httpbin"}, rules=[AuthorizationRule()],
    )
    cg._add_deny_policy_labels(
        g, authorization_policies=[ap], deployments=deployments, services=services,
        mesh_root_namespace="istio-system",
    )
    graph = g.build()
    edge = next(
        e for e in graph["edges"]
        if e["source"] == "service:default/httpbin" and e["target"] == "deployment:default/httpbin"
    )
    assert edge["attributes"]["deny_policies"] == [{
        "name": "default-deny",
        "namespace": "default",
        "action": "DENY",
        "implicit_deny_all": False,
        "rules": [{"from_namespaces": [], "from_principals": [], "to_hosts": []}],
    }]


def test_add_deny_policy_labels_covers_official_istio_allow_shaped_deny_all(cg):
    # Das offizielle Istio-Muster für "alles verbieten" ist KEINE
    # action=DENY-Policy, sondern eine ALLOW-Policy ganz ohne Regeln
    # (spec: {} -> action fehlt, defaultet laut istio.py auf "ALLOW", rules
    # ist eine leere Liste). Ohne jede Regel matcht nichts, also ist nichts
    # erlaubt -> de facto ein Verbot. Genau dieses Cluster-übliche Muster war
    # der eigentliche Bug: es wurde bisher als action != "DENY" komplett
    # übersprungen.
    g = cg.GraphBuilder()
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})]
    g.add_node("deployment", "httpbin", "default")
    g.add_node("service", "httpbin", "default")
    g.add_edge("service:default/httpbin", "deployment:default/httpbin", "selects")
    ap = AuthorizationPolicyInfo(name="deny-all", namespace="default", action="ALLOW")
    cg._add_deny_policy_labels(
        g, authorization_policies=[ap], deployments=deployments, services=services,
        mesh_root_namespace="istio-system",
    )
    graph = g.build()
    edge = next(
        e for e in graph["edges"]
        if e["source"] == "service:default/httpbin" and e["target"] == "deployment:default/httpbin"
    )
    assert edge["attributes"]["deny_policies"] == [{
        "name": "deny-all",
        "namespace": "default",
        "action": "ALLOW",
        "implicit_deny_all": True,
        "rules": [],
    }]


def test_add_deny_policy_labels_ignores_allow_with_meaningful_rules(cg):
    # Gegenprobe: eine ALLOW-Policy MIT tatsächlichem Regelinhalt ist eine
    # echte Erlaubnis, kein implizites Verbot -> gehört nicht zu deny_policies
    # (die gehört zu allow_policies, siehe _add_allow_policy_labels).
    g = cg.GraphBuilder()
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})]
    g.add_node("deployment", "httpbin", "default")
    g.add_node("service", "httpbin", "default")
    g.add_edge("service:default/httpbin", "deployment:default/httpbin", "selects")
    ap = AuthorizationPolicyInfo(
        name="allow-sleep", namespace="default", action="ALLOW", selector={"app": "httpbin"},
        rules=[AuthorizationRule(from_namespaces=["default"])],
    )
    cg._add_deny_policy_labels(
        g, authorization_policies=[ap], deployments=deployments, services=services,
        mesh_root_namespace="istio-system",
    )
    graph = g.build()
    edge = next(
        e for e in graph["edges"]
        if e["source"] == "service:default/httpbin" and e["target"] == "deployment:default/httpbin"
    )
    assert "deny_policies" not in edge["attributes"]


def test_add_deny_policy_labels_does_not_add_edges_or_nodes(cg):
    g = cg.GraphBuilder()
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    g.add_node("deployment", "httpbin", "default")
    ap = AuthorizationPolicyInfo(
        name="deny-sleep", namespace="default", action="DENY", selector={"app": "httpbin"},
        rules=[AuthorizationRule(from_namespaces=["default"])],
    )
    cg._add_deny_policy_labels(
        g, authorization_policies=[ap], deployments=deployments, services=[],
        mesh_root_namespace="istio-system",
    )
    graph = g.build()
    assert graph["edges"] == []
    assert [n["id"] for n in graph["nodes"]] == ["deployment:default/httpbin"]


def test_add_deny_policy_labels_ignores_allow_policies(cg):
    g = cg.GraphBuilder()
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})]
    g.add_node("deployment", "httpbin", "default")
    g.add_node("service", "httpbin", "default")
    g.add_edge("service:default/httpbin", "deployment:default/httpbin", "selects")
    ap = AuthorizationPolicyInfo(
        name="allow-sleep", namespace="default", action="ALLOW", selector={"app": "httpbin"},
        rules=[AuthorizationRule(from_namespaces=["default"])],
    )
    cg._add_deny_policy_labels(
        g, authorization_policies=[ap], deployments=deployments, services=services,
        mesh_root_namespace="istio-system",
    )
    graph = g.build()
    edge = next(
        e for e in graph["edges"]
        if e["source"] == "service:default/httpbin" and e["target"] == "deployment:default/httpbin"
    )
    assert "deny_policies" not in edge["attributes"]


def test_add_deny_policy_labels_covers_namespace_wide_auto_deny_all_baseline(cg):
    # Die "echte" Auto-Deny-All-Baseline: ganz ohne Selector/targetRefs und
    # ganz ohne Regeln (kein einzelner leerer Regel-Eintrag, sondern gar
    # keiner) -> gilt laut _authz_policy_targets für den gesamten Namespace.
    # Genau dieser Fall darf beim Labeln nicht verloren gehen.
    g = cg.GraphBuilder()
    deployments = [
        kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"}),
        kubectl.DeploymentInfo(name="sleep", namespace="default", labels={"app": "sleep"}),
        kubectl.DeploymentInfo(name="other", namespace="other-ns", labels={"app": "other"}),
    ]
    services = [
        kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"}),
        kubectl.ServiceInfo(name="sleep", namespace="default", selector={"app": "sleep"}),
        kubectl.ServiceInfo(name="other", namespace="other-ns", selector={"app": "other"}),
    ]
    for d in deployments:
        g.add_node("deployment", d.name, d.namespace)
    for s in services:
        g.add_node("service", s.name, s.namespace)
        g.add_edge(f"service:{s.namespace}/{s.name}", f"deployment:{s.namespace}/{s.name}", "selects")

    # action="ALLOW" ohne Regeln, wie es istio.py für spec: {} (kein
    # action-Feld) tatsächlich liefert -> das reale Cluster-Muster.
    ap = AuthorizationPolicyInfo(name="default-deny-all", namespace="default", action="ALLOW")
    cg._add_deny_policy_labels(
        g, authorization_policies=[ap], deployments=deployments, services=services,
        mesh_root_namespace="istio-system",
    )
    graph = g.build()
    labeled_targets = {
        e["target"] for e in graph["edges"] if "deny_policies" in e["attributes"]
    }
    assert labeled_targets == {"deployment:default/httpbin", "deployment:default/sleep"}
    other_edge = next(e for e in graph["edges"] if e["target"] == "deployment:other-ns/other")
    assert "deny_policies" not in other_edge["attributes"]


def test_add_deny_policy_labels_covers_mesh_wide_auto_deny_all_baseline(cg):
    # Dieselbe Baseline im Root-Namespace gilt laut _authz_policy_targets
    # mesh-weit für alle Deployments in allen Namespaces.
    g = cg.GraphBuilder()
    deployments = [
        kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"}),
        kubectl.DeploymentInfo(name="other", namespace="other-ns", labels={"app": "other"}),
    ]
    services = [
        kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"}),
        kubectl.ServiceInfo(name="other", namespace="other-ns", selector={"app": "other"}),
    ]
    for d in deployments:
        g.add_node("deployment", d.name, d.namespace)
    for s in services:
        g.add_node("service", s.name, s.namespace)
        g.add_edge(f"service:{s.namespace}/{s.name}", f"deployment:{s.namespace}/{s.name}", "selects")

    ap = AuthorizationPolicyInfo(name="default-deny-all", namespace="istio-system", action="ALLOW")
    cg._add_deny_policy_labels(
        g, authorization_policies=[ap], deployments=deployments, services=services,
        mesh_root_namespace="istio-system",
    )
    graph = g.build()
    labeled_targets = {
        e["target"] for e in graph["edges"] if "deny_policies" in e["attributes"]
    }
    assert labeled_targets == {"deployment:default/httpbin", "deployment:other-ns/other"}


# ---------------------------------------------------------------------------
# build_graph (integration, mit gemockter Datenerfassungsschicht)
# ---------------------------------------------------------------------------

def test_build_graph_direct_service_call(cg, monkeypatch):
    deployments = [
        kubectl.DeploymentInfo(name="sleep", namespace="default", labels={"app": "sleep"}, service_account="sleep"),
        kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"}),
    ]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})]

    monkeypatch.setattr(cg, "get_mesh_root_namespace", lambda: "istio-system")
    monkeypatch.setattr(cg, "get_deployments", lambda namespace=None: deployments)
    monkeypatch.setattr(cg, "get_services", lambda namespace=None: services)
    monkeypatch.setattr(cg, "get_istio_resources", lambda namespace=None: IstioResources())

    graph = cg.build_graph(namespace=None)
    edge_triples = {(e["source"], e["target"], e["relation"]) for e in graph["edges"]}
    assert ("deployment:default/sleep", "service:default/httpbin", "may_call") in edge_triples
    assert ("service:default/httpbin", "deployment:default/httpbin", "selects") in edge_triples
    httpbin_node = next(n for n in graph["nodes"] if n["id"] == "deployment:default/httpbin")
    assert httpbin_node["attributes"]["service_account"] is None


def test_build_graph_virtual_service_routes_through_gateway_to_service_entry(cg, monkeypatch):
    deployments = [kubectl.DeploymentInfo(name="vm-proxy", namespace="default", labels={"app": "vm-proxy"})]
    services: list[kubectl.ServiceInfo] = []
    resources = IstioResources(
        gateways=[GatewayInfo(
            name="ingress", namespace="istio-system", selector={"istio": "ingressgateway"},
            servers=[GatewayServer(hosts=["api.example.com"], port_number=443, protocol="HTTPS", tls_mode=None)],
        )],
        virtual_services=[VirtualServiceInfo(
            name="api", namespace="default", hosts=["api.example.com"], gateways=["istio-system/ingress"],
            destinations=[RouteDestination(protocol="http", host="external.example.com")],
        )],
        service_entries=[ServiceEntryInfo(
            name="external", namespace="default", hosts=["external.example.com"],
            workload_selector={"app": "vm-proxy"},
        )],
    )

    monkeypatch.setattr(cg, "get_mesh_root_namespace", lambda: "istio-system")
    monkeypatch.setattr(cg, "get_deployments", lambda namespace=None: deployments)
    monkeypatch.setattr(cg, "get_services", lambda namespace=None: services)
    monkeypatch.setattr(cg, "get_istio_resources", lambda namespace=None: resources)

    graph = cg.build_graph(namespace=None)
    edge_triples = {(e["source"], e["target"], e["relation"]) for e in graph["edges"]}
    assert ("gateway:istio-system/ingress", "virtualservice:default/api", "exposes") in edge_triples
    assert ("virtualservice:default/api", "serviceentry:default/external", "routes_to") in edge_triples
    assert ("serviceentry:default/external", "deployment:default/vm-proxy", "resolves_to") in edge_triples
    # Da vs.gateways explizit gesetzt ist (kein "mesh"), darf es keine
    # mesh-weite Deployment->VirtualService-Kante geben.
    assert not any(r == "routes_via" for _, _, r in edge_triples)


def test_build_graph_prunes_service_entry_without_workload_selector(cg, monkeypatch):
    deployments = [kubectl.DeploymentInfo(name="sleep", namespace="default", labels={"app": "sleep"})]
    resources = IstioResources(
        service_entries=[ServiceEntryInfo(name="external", namespace="default", hosts=["external.example.com"])],
    )

    monkeypatch.setattr(cg, "get_mesh_root_namespace", lambda: "istio-system")
    monkeypatch.setattr(cg, "get_deployments", lambda namespace=None: deployments)
    monkeypatch.setattr(cg, "get_services", lambda namespace=None: [])
    monkeypatch.setattr(cg, "get_istio_resources", lambda namespace=None: resources)

    graph = cg.build_graph(namespace=None)
    assert graph["edges"] == []
    assert [n["id"] for n in graph["nodes"]] == ["deployment:default/sleep"]


def test_build_graph_default_deny_keeps_selects_edge_but_labels_it(cg, monkeypatch):
    # Anders als in der früheren Auto-Deny-Version wird die unbedingte
    # "selects"-Kante durch eine default-deny Baseline-Policy nicht mehr
    # entfernt — stattdessen bleibt sie erhalten und bekommt zusätzlich ein
    # deny_policies-Label, das die Baseline-Policy benennt.
    deployments = [kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"})]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})]
    resources = IstioResources(
        authorization_policies=[AuthorizationPolicyInfo(
            name="default-deny", namespace="default", action="DENY", rules=[AuthorizationRule()],
        )],
    )

    monkeypatch.setattr(cg, "get_mesh_root_namespace", lambda: "istio-system")
    monkeypatch.setattr(cg, "get_deployments", lambda namespace=None: deployments)
    monkeypatch.setattr(cg, "get_services", lambda namespace=None: services)
    monkeypatch.setattr(cg, "get_istio_resources", lambda namespace=None: resources)

    graph = cg.build_graph(namespace=None)
    edge = next(
        e for e in graph["edges"]
        if e["source"] == "service:default/httpbin" and e["target"] == "deployment:default/httpbin"
    )
    assert edge["relation"] == "selects"
    assert edge["attributes"]["deny_policies"][0]["name"] == "default-deny"


def test_build_graph_deny_and_allow_labels_coexist_on_same_edge(cg, monkeypatch):
    deployments = [
        kubectl.DeploymentInfo(name="sleep", namespace="default", labels={"app": "sleep"}, service_account="sleep"),
        kubectl.DeploymentInfo(name="httpbin", namespace="default", labels={"app": "httpbin"}),
    ]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", selector={"app": "httpbin"})]
    resources = IstioResources(
        authorization_policies=[
            AuthorizationPolicyInfo(
                name="default-deny", namespace="default", action="DENY", rules=[AuthorizationRule()],
            ),
            AuthorizationPolicyInfo(
                name="allow-sleep", namespace="default", action="ALLOW", selector={"app": "httpbin"},
                rules=[AuthorizationRule(from_principals=["cluster.local/ns/default/sa/sleep"])],
            ),
        ],
    )

    monkeypatch.setattr(cg, "get_mesh_root_namespace", lambda: "istio-system")
    monkeypatch.setattr(cg, "get_deployments", lambda namespace=None: deployments)
    monkeypatch.setattr(cg, "get_services", lambda namespace=None: services)
    monkeypatch.setattr(cg, "get_istio_resources", lambda namespace=None: resources)

    graph = cg.build_graph(namespace=None)
    edge = next(
        e for e in graph["edges"]
        if e["source"] == "service:default/httpbin" and e["target"] == "deployment:default/httpbin"
    )
    assert edge["attributes"]["deny_policies"][0]["name"] == "default-deny"
    assert edge["attributes"]["allow_policies"][0]["name"] == "allow-sleep"
