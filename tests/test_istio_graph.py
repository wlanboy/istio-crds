from __future__ import annotations

import dataclasses

import pytest

import kubectl
from istio import IstioResources, TargetRef, VirtualServiceInfo


@pytest.fixture
def ig(load_module):
    return load_module("istio_graph", "istio-graph.py")


# ---------------------------------------------------------------------------
# _selector_matches / _host_matches_service / _parse_principal
# ---------------------------------------------------------------------------

def test_selector_matches_empty_selector_never_matches(ig):
    assert ig._selector_matches({}, {"app": "foo"}) is False


def test_selector_matches_all_keys_present(ig):
    assert ig._selector_matches({"app": "foo"}, {"app": "foo", "version": "v1"}) is True


def test_selector_matches_missing_key(ig):
    assert ig._selector_matches({"app": "foo", "tier": "backend"}, {"app": "foo"}) is False


@pytest.mark.parametrize("host,expected", [
    ("httpbin", True),
    ("httpbin.default", True),
    ("httpbin.default.svc.cluster.local", True),
    ("other-svc", False),
    ("*", False),
    ("", False),
])
def test_host_matches_service(ig, host, expected):
    svc = kubectl.ServiceInfo(name="httpbin", namespace="default")
    assert ig._host_matches_service(host, svc) is expected


def test_parse_principal_valid(ig):
    assert ig._parse_principal("cluster.local/ns/default/sa/sleep") == ("default", "sleep")


def test_parse_principal_invalid(ig):
    assert ig._parse_principal("not-a-principal") is None


# ---------------------------------------------------------------------------
# GraphBuilder
# ---------------------------------------------------------------------------

def test_graph_builder_add_node_dedups_by_id(ig):
    g = ig.GraphBuilder()
    id1 = g.add_node("service", "httpbin", "default", ports=[80])
    id2 = g.add_node("service", "httpbin", "default", ports=[9080])
    assert id1 == id2
    graph = g.build()
    assert len(graph["nodes"]) == 1
    assert graph["nodes"][0]["attributes"] == {"ports": [80]}


def test_graph_builder_add_node_cluster_scoped_has_no_namespace_segment(ig):
    g = ig.GraphBuilder()
    node_id = g.add_node("namespace", "default")
    assert node_id == "namespace:default"


def test_graph_builder_add_edge_dropped_when_endpoint_missing(ig):
    g = ig.GraphBuilder()
    g.add_node("service", "a", "default")
    g.add_edge("service:default/a", "service:default/missing", "selects")
    assert g.build()["edges"] == []


def test_graph_builder_add_edge_kept_when_both_endpoints_exist(ig):
    g = ig.GraphBuilder()
    g.add_node("service", "a", "default")
    g.add_node("pod", "a-1", "default")
    g.add_edge("service:default/a", "pod:default/a-1", "selects")
    edges = g.build()["edges"]
    assert len(edges) == 1
    assert edges[0]["relation"] == "selects"


def test_graph_builder_build_sorts_nodes_by_id(ig):
    g = ig.GraphBuilder()
    g.add_node("service", "z", "default")
    g.add_node("service", "a", "default")
    node_ids = [n["id"] for n in g.build()["nodes"]]
    assert node_ids == sorted(node_ids)


# ---------------------------------------------------------------------------
# _workload_scope_edges
# ---------------------------------------------------------------------------

def test_workload_scope_edges_empty_selector_applies_to_own_namespace(ig):
    g = ig.GraphBuilder()
    g.add_node("sidecar", "default", "ns1")
    g.add_node("namespace", "ns1")
    ig._workload_scope_edges(g, node_id="sidecar:ns1/default", namespace="ns1", selector={}, pods=[])
    edges = g.build()["edges"]
    assert edges == [{"source": "sidecar:ns1/default", "target": "namespace:ns1", "relation": "applies_to_namespace", "attributes": {}}]


def test_workload_scope_edges_empty_selector_mesh_root_applies_mesh_wide(ig):
    g = ig.GraphBuilder()
    g.add_node("peerauthentication", "default", "istio-system")
    g.add_node("namespace", "istio-system")
    g.add_node("namespace", "ns1")
    namespaces = [kubectl.NamespaceInfo(name="istio-system"), kubectl.NamespaceInfo(name="ns1")]
    ig._workload_scope_edges(
        g, node_id="peerauthentication:istio-system/default", namespace="istio-system", selector={}, pods=[],
        namespaces=namespaces, mesh_root_namespace="istio-system",
    )
    edges = g.build()["edges"]
    targets = {e["target"] for e in edges}
    assert targets == {"namespace:istio-system", "namespace:ns1"}
    assert all(e["attributes"].get("mesh_wide") is True for e in edges)


def test_workload_scope_edges_selector_matches_pods_and_workload_entries(ig):
    from istio import WorkloadEntryInfo
    g = ig.GraphBuilder()
    g.add_node("sidecar", "x", "default")
    g.add_node("pod", "httpbin-1", "default")
    g.add_node("workloadentry", "vm-1", "default")
    pods = [kubectl.PodInfo(name="httpbin-1", namespace="default", labels={"app": "httpbin"})]
    wes = [WorkloadEntryInfo(name="vm-1", namespace="default", labels={"app": "httpbin"})]
    ig._workload_scope_edges(
        g, node_id="sidecar:default/x", namespace="default", selector={"app": "httpbin"}, pods=pods,
        workload_entries=wes,
    )
    edges = g.build()["edges"]
    targets = {(e["target"], e["relation"]) for e in edges}
    assert ("pod:default/httpbin-1", "applies_to") in targets
    assert ("workloadentry:default/vm-1", "applies_to") in targets


# ---------------------------------------------------------------------------
# _gateway_selector_edges
# ---------------------------------------------------------------------------

def test_gateway_selector_edges_matches_pods_across_namespaces(ig):
    g = ig.GraphBuilder()
    g.add_node("gateway", "gw", "app-ns")
    g.add_node("pod", "ingressgateway-1", "istio-system")
    pods = [kubectl.PodInfo(name="ingressgateway-1", namespace="istio-system", labels={"istio": "ingressgateway"})]
    ig._gateway_selector_edges(g, node_id="gateway:app-ns/gw", selector={"istio": "ingressgateway"}, pods=pods, workload_entries=[])
    edges = g.build()["edges"]
    assert edges[0]["target"] == "pod:istio-system/ingressgateway-1"
    assert edges[0]["relation"] == "selects"


# ---------------------------------------------------------------------------
# _add_export_to_edges
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("export_to", [[], ["*"]])
def test_add_export_to_edges_wildcard_or_empty_exports_to_all(ig, export_to):
    g = ig.GraphBuilder()
    g.add_node("virtualservice", "vs", "default")
    g.add_node("namespace", "default")
    g.add_node("namespace", "other")
    namespaces = [kubectl.NamespaceInfo(name="default"), kubectl.NamespaceInfo(name="other")]
    ig._add_export_to_edges(g, node_id="virtualservice:default/vs", namespace="default", export_to=export_to, namespaces=namespaces)
    targets = {e["target"] for e in g.build()["edges"]}
    assert targets == {"namespace:default", "namespace:other"}


def test_add_export_to_edges_dot_means_own_namespace(ig):
    g = ig.GraphBuilder()
    g.add_node("virtualservice", "vs", "default")
    g.add_node("namespace", "default")
    g.add_node("namespace", "other")
    namespaces = [kubectl.NamespaceInfo(name="default"), kubectl.NamespaceInfo(name="other")]
    ig._add_export_to_edges(g, node_id="virtualservice:default/vs", namespace="default", export_to=["."], namespaces=namespaces)
    targets = {e["target"] for e in g.build()["edges"]}
    assert targets == {"namespace:default"}


def test_add_export_to_edges_explicit_namespace(ig):
    g = ig.GraphBuilder()
    g.add_node("virtualservice", "vs", "default")
    g.add_node("namespace", "other")
    namespaces = [kubectl.NamespaceInfo(name="default"), kubectl.NamespaceInfo(name="other")]
    ig._add_export_to_edges(g, node_id="virtualservice:default/vs", namespace="default", export_to=["other"], namespaces=namespaces)
    targets = {e["target"] for e in g.build()["edges"]}
    assert targets == {"namespace:other"}


# ---------------------------------------------------------------------------
# _add_target_ref_edges
# ---------------------------------------------------------------------------

def test_add_target_ref_edges_known_kind(ig):
    g = ig.GraphBuilder()
    g.add_node("authorizationpolicy", "ap", "default")
    g.add_node("gateway", "gw", "default")
    ig._add_target_ref_edges(g, node_id="authorizationpolicy:default/ap", namespace="default", target_refs=[TargetRef(kind="Gateway", name="gw")])
    edges = g.build()["edges"]
    assert edges == [{"source": "authorizationpolicy:default/ap", "target": "gateway:default/gw", "relation": "targets", "attributes": {}}]


def test_add_target_ref_edges_unknown_kind_skipped(ig):
    g = ig.GraphBuilder()
    g.add_node("authorizationpolicy", "ap", "default")
    ig._add_target_ref_edges(g, node_id="authorizationpolicy:default/ap", namespace="default", target_refs=[TargetRef(kind="Frobnicator", name="x")])
    assert g.build()["edges"] == []


# ---------------------------------------------------------------------------
# _network_policy_peer_targets
# ---------------------------------------------------------------------------

def test_network_policy_peer_targets_ip_block(ig):
    peer = kubectl.NetworkPolicyPeer(ip_block_cidr="10.0.0.0/8")
    result = ig._network_policy_peer_targets(peer, policy_namespace="default", namespaces=[], pods=[])
    assert result == ["cidr:10.0.0.0/8"]


def test_network_policy_peer_targets_defaults_to_policy_namespace(ig):
    peer = kubectl.NetworkPolicyPeer()
    result = ig._network_policy_peer_targets(peer, policy_namespace="default", namespaces=[], pods=[])
    assert result == ["namespace:default"]


def test_network_policy_peer_targets_namespace_selector(ig):
    peer = kubectl.NetworkPolicyPeer(namespace_selector={"env": "prod"})
    namespaces = [kubectl.NamespaceInfo(name="ns-prod", labels={"env": "prod"}), kubectl.NamespaceInfo(name="ns-dev", labels={"env": "dev"})]
    result = ig._network_policy_peer_targets(peer, policy_namespace="default", namespaces=namespaces, pods=[])
    assert result == ["namespace:ns-prod"]


def test_network_policy_peer_targets_pod_selector_filters_by_namespace(ig):
    peer = kubectl.NetworkPolicyPeer(pod_selector={"app": "httpbin"})
    pods = [
        kubectl.PodInfo(name="httpbin-1", namespace="default", labels={"app": "httpbin"}),
        kubectl.PodInfo(name="httpbin-2", namespace="other", labels={"app": "httpbin"}),
    ]
    result = ig._network_policy_peer_targets(peer, policy_namespace="default", namespaces=[], pods=pods)
    assert result == ["pod:default/httpbin-1"]


# ---------------------------------------------------------------------------
# build_graph (integration, with mocked data-collection layer)
# ---------------------------------------------------------------------------

def test_build_graph_wires_service_to_pod(ig, monkeypatch):
    namespaces = [kubectl.NamespaceInfo(name="default")]
    services = [kubectl.ServiceInfo(name="httpbin", namespace="default", ports=[80], selector={"app": "httpbin"})]
    pods = [kubectl.PodInfo(name="httpbin-1", namespace="default", labels={"app": "httpbin"}, service_account="httpbin")]
    service_accounts = [kubectl.ServiceAccountInfo(name="httpbin", namespace="default")]

    monkeypatch.setattr(ig, "get_mesh_root_namespace", lambda: "istio-system")
    monkeypatch.setattr(ig, "get_namespaces", lambda namespace=None: namespaces)
    monkeypatch.setattr(ig, "get_services", lambda namespace=None: services)
    monkeypatch.setattr(ig, "get_service_accounts", lambda namespace=None: service_accounts)
    monkeypatch.setattr(ig, "get_pods", lambda namespace=None: pods)
    monkeypatch.setattr(ig, "get_network_policies", lambda namespace=None: [])
    monkeypatch.setattr(ig, "get_istio_resources", lambda namespace=None: IstioResources())
    monkeypatch.setattr(ig, "get_hosts", lambda resources: [])

    graph = ig.build_graph(namespace=None)
    assert {"nodes", "edges"} == graph.keys()
    edge_pairs = {(e["source"], e["target"], e["relation"]) for e in graph["edges"]}
    assert ("service:default/httpbin", "pod:default/httpbin-1", "selects") in edge_pairs
    assert ("pod:default/httpbin-1", "serviceaccount:default/httpbin", "uses_service_account") in edge_pairs
