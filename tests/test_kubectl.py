from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client import (
    V1ConfigMap,
    V1CustomResourceDefinition,
    V1CustomResourceDefinitionCondition,
    V1CustomResourceDefinitionList,
    V1CustomResourceDefinitionNames,
    V1CustomResourceDefinitionSpec,
    V1CustomResourceDefinitionStatus,
    V1CustomResourceDefinitionVersion,
    V1IPBlock,
    V1LabelSelector,
    V1Namespace,
    V1NamespaceList,
    V1NetworkPolicy,
    V1NetworkPolicyEgressRule,
    V1NetworkPolicyIngressRule,
    V1NetworkPolicyList,
    V1NetworkPolicyPeer,
    V1NetworkPolicyPort,
    V1NetworkPolicySpec,
    V1ObjectMeta,
    V1Pod,
    V1PodList,
    V1PodSpec,
    V1Service,
    V1ServiceAccount,
    V1ServiceAccountList,
    V1ServiceList,
    V1ServicePort,
    V1ServiceSpec,
)
from kubernetes.client.rest import ApiException

import kubectl


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def test_load_config_prefers_incluster():
    with patch.object(kubectl.config, "load_incluster_config") as incluster, \
            patch.object(kubectl.config, "load_kube_config") as kube, \
            patch.object(kubectl.client.Configuration, "get_default_copy") as get_default, \
            patch.object(kubectl.client.Configuration, "set_default") as set_default:
        cfg = MagicMock()
        get_default.return_value = cfg
        kubectl.load_config()
        incluster.assert_called_once()
        kube.assert_not_called()
        set_default.assert_called_once_with(cfg)
        assert cfg.retries == 0


def test_load_config_falls_back_to_kube_config():
    with patch.object(kubectl.config, "load_incluster_config", side_effect=kubectl.config.ConfigException), \
            patch.object(kubectl.config, "load_kube_config") as kube, \
            patch.object(kubectl.client.Configuration, "get_default_copy") as get_default, \
            patch.object(kubectl.client.Configuration, "set_default"):
        get_default.return_value = MagicMock()
        kubectl.load_config()
        kube.assert_called_once()


def test_load_config_insecure_disables_verification():
    with patch.object(kubectl.config, "load_incluster_config"), \
            patch.object(kubectl.client.Configuration, "get_default_copy") as get_default, \
            patch.object(kubectl.client.Configuration, "set_default") as set_default:
        cfg = MagicMock()
        get_default.return_value = cfg
        kubectl.load_config(verify_ssl=False)
        assert cfg.verify_ssl is False
        set_default.assert_called_once_with(cfg)


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------

def test_get_namespaces_single():
    ns = V1Namespace(metadata=V1ObjectMeta(name="foo", labels={"istio-injection": "enabled"}))
    v1 = MagicMock()
    v1.read_namespace.return_value = ns
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        result = kubectl.get_namespaces(namespace="foo")
    assert result == [kubectl.NamespaceInfo(name="foo", labels={"istio-injection": "enabled"})]
    v1.read_namespace.assert_called_once_with("foo", _request_timeout=kubectl._REQUEST_TIMEOUT)


def test_get_namespaces_all_and_missing_labels():
    ns_list = V1NamespaceList(items=[
        V1Namespace(metadata=V1ObjectMeta(name="default")),
        V1Namespace(metadata=V1ObjectMeta(name="istio-system", labels={"a": "b"})),
    ])
    v1 = MagicMock()
    v1.list_namespace.return_value = ns_list
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        result = kubectl.get_namespaces()
    assert result == [
        kubectl.NamespaceInfo(name="default", labels={}),
        kubectl.NamespaceInfo(name="istio-system", labels={"a": "b"}),
    ]


# ---------------------------------------------------------------------------
# Mesh root namespace
# ---------------------------------------------------------------------------

def test_get_mesh_root_namespace_reads_configured_value():
    cm = V1ConfigMap(data={"mesh": "rootNamespace: istio-control\n"})
    v1 = MagicMock()
    v1.read_namespaced_config_map.return_value = cm
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        assert kubectl.get_mesh_root_namespace() == "istio-control"


def test_get_mesh_root_namespace_defaults_when_configmap_missing():
    v1 = MagicMock()
    v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        assert kubectl.get_mesh_root_namespace() == "istio-system"


def test_get_mesh_root_namespace_defaults_when_mesh_key_missing():
    cm = V1ConfigMap(data={})
    v1 = MagicMock()
    v1.read_namespaced_config_map.return_value = cm
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        assert kubectl.get_mesh_root_namespace() == "istio-system"


def test_get_mesh_root_namespace_defaults_on_invalid_yaml():
    cm = V1ConfigMap(data={"mesh": "rootNamespace: [unterminated"})
    v1 = MagicMock()
    v1.read_namespaced_config_map.return_value = cm
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        assert kubectl.get_mesh_root_namespace() == "istio-system"


def test_get_mesh_root_namespace_defaults_when_rootnamespace_empty():
    cm = V1ConfigMap(data={"mesh": "rootNamespace: \n"})
    v1 = MagicMock()
    v1.read_namespaced_config_map.return_value = cm
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        assert kubectl.get_mesh_root_namespace() == "istio-system"


def test_get_mesh_root_namespace_custom_istio_namespace():
    v1 = MagicMock()
    v1.read_namespaced_config_map.side_effect = ApiException(status=404)
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        assert kubectl.get_mesh_root_namespace("my-istio-ns") == "my-istio-ns"
    v1.read_namespaced_config_map.assert_called_once_with(
        "istio", "my-istio-ns", _request_timeout=kubectl._REQUEST_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------

def test_get_services_namespaced():
    svc = V1Service(
        metadata=V1ObjectMeta(name="httpbin", namespace="default"),
        spec=V1ServiceSpec(ports=[V1ServicePort(port=80), V1ServicePort(port=443)], selector={"app": "httpbin"}),
    )
    v1 = MagicMock()
    v1.list_namespaced_service.return_value = V1ServiceList(items=[svc])
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        result = kubectl.get_services(namespace="default")
    assert result == [kubectl.ServiceInfo(name="httpbin", namespace="default", ports=[80, 443], selector={"app": "httpbin"})]
    v1.list_namespaced_service.assert_called_once()
    v1.list_service_for_all_namespaces.assert_not_called()


def test_get_services_all_namespaces_handles_missing_spec():
    svc = V1Service(metadata=V1ObjectMeta(name="headless", namespace="default"), spec=None)
    v1 = MagicMock()
    v1.list_service_for_all_namespaces.return_value = V1ServiceList(items=[svc])
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        result = kubectl.get_services()
    assert result == [kubectl.ServiceInfo(name="headless", namespace="default", ports=[], selector={})]


# ---------------------------------------------------------------------------
# Service accounts
# ---------------------------------------------------------------------------

def test_get_service_accounts_namespaced():
    sa = V1ServiceAccount(metadata=V1ObjectMeta(name="httpbin", namespace="default"))
    v1 = MagicMock()
    v1.list_namespaced_service_account.return_value = V1ServiceAccountList(items=[sa])
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        result = kubectl.get_service_accounts(namespace="default")
    assert result == [kubectl.ServiceAccountInfo(name="httpbin", namespace="default")]


def test_get_service_accounts_all_namespaces():
    sa = V1ServiceAccount(metadata=V1ObjectMeta(name="default", namespace="kube-system"))
    v1 = MagicMock()
    v1.list_service_account_for_all_namespaces.return_value = V1ServiceAccountList(items=[sa])
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        result = kubectl.get_service_accounts()
    assert result == [kubectl.ServiceAccountInfo(name="default", namespace="kube-system")]


# ---------------------------------------------------------------------------
# Pods
# ---------------------------------------------------------------------------

def test_get_pods_extracts_service_account():
    pod = V1Pod(
        metadata=V1ObjectMeta(name="httpbin-1", namespace="default", labels={"app": "httpbin"}),
        spec=V1PodSpec(containers=[], service_account_name="httpbin"),
    )
    v1 = MagicMock()
    v1.list_namespaced_pod.return_value = V1PodList(items=[pod])
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        result = kubectl.get_pods(namespace="default")
    assert result == [
        kubectl.PodInfo(name="httpbin-1", namespace="default", labels={"app": "httpbin"}, service_account="httpbin"),
    ]


def test_get_pods_handles_missing_spec():
    pod = V1Pod(metadata=V1ObjectMeta(name="ghost", namespace="default"), spec=None)
    v1 = MagicMock()
    v1.list_pod_for_all_namespaces.return_value = V1PodList(items=[pod])
    with patch.object(kubectl.client, "CoreV1Api", return_value=v1):
        result = kubectl.get_pods()
    assert result == [kubectl.PodInfo(name="ghost", namespace="default", labels={}, service_account=None)]


# ---------------------------------------------------------------------------
# NetworkPolicies
# ---------------------------------------------------------------------------

def test_network_policy_peer_pod_selector():
    peer = V1NetworkPolicyPeer(pod_selector=V1LabelSelector(match_labels={"app": "httpbin"}))
    result = kubectl._network_policy_peer(peer)
    assert result == kubectl.NetworkPolicyPeer(pod_selector={"app": "httpbin"}, namespace_selector={}, ip_block_cidr=None)


def test_network_policy_peer_ip_block():
    peer = V1NetworkPolicyPeer(ip_block=V1IPBlock(cidr="10.0.0.0/8"))
    result = kubectl._network_policy_peer(peer)
    assert result.ip_block_cidr == "10.0.0.0/8"
    assert result.pod_selector == {}


def test_network_policy_port():
    port = V1NetworkPolicyPort(protocol="TCP", port=8080)
    result = kubectl._network_policy_port(port)
    assert result == kubectl.NetworkPolicyPort(protocol="TCP", port="8080", end_port=None)


def test_network_policy_port_none():
    port = V1NetworkPolicyPort()
    result = kubectl._network_policy_port(port)
    assert result.port is None


def test_get_network_policies_parses_ingress_and_egress():
    np = V1NetworkPolicy(
        metadata=V1ObjectMeta(name="deny-all", namespace="default"),
        spec=V1NetworkPolicySpec(
            pod_selector=V1LabelSelector(match_labels={"app": "httpbin"}),
            policy_types=["Ingress", "Egress"],
            ingress=[V1NetworkPolicyIngressRule(
                _from=[V1NetworkPolicyPeer(pod_selector=V1LabelSelector(match_labels={"app": "sleep"}))],
                ports=[V1NetworkPolicyPort(protocol="TCP", port=80)],
            )],
            egress=[V1NetworkPolicyEgressRule(
                to=[V1NetworkPolicyPeer(ip_block=V1IPBlock(cidr="0.0.0.0/0"))],
                ports=[],
            )],
        ),
    )
    net = MagicMock()
    net.list_namespaced_network_policy.return_value = V1NetworkPolicyList(items=[np])
    with patch.object(kubectl.client, "NetworkingV1Api", return_value=net):
        result = kubectl.get_network_policies(namespace="default")
    assert len(result) == 1
    info = result[0]
    assert info.name == "deny-all"
    assert info.pod_selector == {"app": "httpbin"}
    assert info.policy_types == ["Ingress", "Egress"]
    assert info.ingress[0].peers[0].pod_selector == {"app": "sleep"}
    assert info.ingress[0].ports[0].port == "80"
    assert info.egress[0].peers[0].ip_block_cidr == "0.0.0.0/0"


def test_get_network_policies_all_namespaces():
    net = MagicMock()
    net.list_network_policy_for_all_namespaces.return_value = V1NetworkPolicyList(items=[])
    with patch.object(kubectl.client, "NetworkingV1Api", return_value=net):
        result = kubectl.get_network_policies()
    assert result == []
    net.list_network_policy_for_all_namespaces.assert_called_once()


# ---------------------------------------------------------------------------
# _custom_list
# ---------------------------------------------------------------------------

def test_custom_list_namespaced():
    custom = MagicMock()
    custom.list_namespaced_custom_object.return_value = {"items": []}
    result = kubectl._custom_list(custom, group="networking.istio.io", version="v1", namespace="default", plural="virtualservices")
    assert result == {"items": []}
    custom.list_namespaced_custom_object.assert_called_once_with(
        group="networking.istio.io", version="v1", namespace="default", plural="virtualservices",
        _request_timeout=kubectl._REQUEST_TIMEOUT,
    )
    custom.list_cluster_custom_object.assert_not_called()


def test_custom_list_cluster_scoped():
    custom = MagicMock()
    custom.list_cluster_custom_object.return_value = {"items": []}
    kubectl._custom_list(custom, group="networking.istio.io", version="v1", namespace=None, plural="gateways")
    custom.list_cluster_custom_object.assert_called_once_with(
        group="networking.istio.io", version="v1", plural="gateways",
        _request_timeout=kubectl._REQUEST_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# CRDVersionedInfo properties
# ---------------------------------------------------------------------------

def test_storage_version_property():
    info = kubectl.CRDVersionedInfo(
        name="x", group="g", kind="X", plural="xs", namespaced=True,
        versions=[
            kubectl.CRDVersionInfo(version="v1alpha1", served=True, storage=False),
            kubectl.CRDVersionInfo(version="v1", served=True, storage=True),
        ],
    )
    assert info.storage_version == "v1"


def test_storage_version_property_none_when_no_storage_version():
    info = kubectl.CRDVersionedInfo(name="x", group="g", kind="X", plural="xs", namespaced=True)
    assert info.storage_version is None


def test_pending_migration_versions():
    info = kubectl.CRDVersionedInfo(
        name="x", group="g", kind="X", plural="xs", namespaced=True,
        versions=[kubectl.CRDVersionInfo(version="v1", served=True, storage=True)],
        stored_versions=["v1alpha1", "v1"],
    )
    assert info.pending_migration_versions == ["v1alpha1"]


def test_pending_migration_versions_empty_when_fully_migrated():
    info = kubectl.CRDVersionedInfo(
        name="x", group="g", kind="X", plural="xs", namespaced=True,
        versions=[kubectl.CRDVersionInfo(version="v1", served=True, storage=True)],
        stored_versions=["v1"],
    )
    assert info.pending_migration_versions == []


# ---------------------------------------------------------------------------
# get_crd_versions
# ---------------------------------------------------------------------------

def _crd(name, group, kind, plural, scope, versions, *, conditions=None, stored_versions=None, conversion_strategy=None):
    status = V1CustomResourceDefinitionStatus(
        stored_versions=stored_versions or [v.name for v in versions if v.storage],
        conditions=conditions or [],
        accepted_names=V1CustomResourceDefinitionNames(kind=kind, plural=plural),
    )
    spec = V1CustomResourceDefinitionSpec(
        group=group,
        names=V1CustomResourceDefinitionNames(kind=kind, plural=plural),
        scope=scope,
        versions=versions,
    )
    if conversion_strategy is not None:
        spec.conversion = kubectl.client.V1CustomResourceConversion(strategy=conversion_strategy)
    return V1CustomResourceDefinition(
        metadata=V1ObjectMeta(name=name), spec=spec, status=status,
    )


def test_get_crd_versions_basic_parsing():
    crd = _crd(
        "virtualservices.networking.istio.io", "networking.istio.io", "VirtualService", "virtualservices",
        "Namespaced",
        [
            V1CustomResourceDefinitionVersion(name="v1alpha3", served=True, storage=False, deprecated=True, deprecation_warning="use v1"),
            V1CustomResourceDefinitionVersion(name="v1", served=True, storage=True),
        ],
        conversion_strategy="Webhook",
    )
    ext = MagicMock()
    ext.list_custom_resource_definition.return_value = V1CustomResourceDefinitionList(items=[crd])
    with patch.object(kubectl.client, "ApiextensionsV1Api", return_value=ext):
        result = kubectl.get_crd_versions()
    assert len(result) == 1
    info = result[0]
    assert info.name == "virtualservices.networking.istio.io"
    assert info.namespaced is True
    assert info.conversion_strategy == "Webhook"
    assert info.established is True
    assert info.names_accepted is True
    assert [v.version for v in info.versions] == ["v1alpha3", "v1"]
    assert info.versions[0].deprecated is True
    assert info.versions[0].deprecation_warning == "use v1"


def test_get_crd_versions_unhealthy_conditions_captured():
    conditions = [
        V1CustomResourceDefinitionCondition(type="Established", status="False", message="names conflict"),
        V1CustomResourceDefinitionCondition(type="NamesAccepted", status="False", message="already in use"),
    ]
    crd = _crd(
        "broken.example.io", "example.io", "Broken", "brokens", "Namespaced",
        [V1CustomResourceDefinitionVersion(name="v1", served=True, storage=True)],
        conditions=conditions,
    )
    ext = MagicMock()
    ext.list_custom_resource_definition.return_value = V1CustomResourceDefinitionList(items=[crd])
    with patch.object(kubectl.client, "ApiextensionsV1Api", return_value=ext):
        result = kubectl.get_crd_versions()
    info = result[0]
    assert info.established is False
    assert info.established_message == "names conflict"
    assert info.names_accepted is False
    assert info.names_accepted_message == "already in use"


def test_get_crd_versions_skips_cluster_scoped_when_namespace_given():
    cluster_crd = _crd(
        "clusterthing.example.io", "example.io", "ClusterThing", "clusterthings", "Cluster",
        [V1CustomResourceDefinitionVersion(name="v1", served=True, storage=True)],
    )
    ns_crd = _crd(
        "nsthing.example.io", "example.io", "NsThing", "nsthings", "Namespaced",
        [V1CustomResourceDefinitionVersion(name="v1", served=True, storage=True)],
    )
    ext = MagicMock()
    ext.list_custom_resource_definition.return_value = V1CustomResourceDefinitionList(items=[cluster_crd, ns_crd])
    with patch.object(kubectl.client, "ApiextensionsV1Api", return_value=ext):
        result = kubectl.get_crd_versions(namespace="default")
    assert [r.name for r in result] == ["nsthing.example.io"]


def test_get_crd_versions_sorted_by_group_and_kind():
    crd_b = _crd("bs.b.io", "b.io", "B", "bs", "Namespaced", [V1CustomResourceDefinitionVersion(name="v1", served=True, storage=True)])
    crd_a = _crd("as.a.io", "a.io", "A", "as", "Namespaced", [V1CustomResourceDefinitionVersion(name="v1", served=True, storage=True)])
    ext = MagicMock()
    ext.list_custom_resource_definition.return_value = V1CustomResourceDefinitionList(items=[crd_b, crd_a])
    with patch.object(kubectl.client, "ApiextensionsV1Api", return_value=ext):
        result = kubectl.get_crd_versions()
    assert [r.group for r in result] == ["a.io", "b.io"]
