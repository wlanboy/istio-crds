from __future__ import annotations

from unittest.mock import MagicMock

from kubernetes.client import V1CustomResourceDefinition, V1CustomResourceDefinitionSpec, V1CustomResourceDefinitionVersion
from kubernetes.client.rest import ApiException

import istio


# ---------------------------------------------------------------------------
# VirtualService parsing
# ---------------------------------------------------------------------------

def test_parse_virtual_service_basic_routes():
    item = {
        "metadata": {"name": "reviews", "namespace": "default"},
        "spec": {
            "hosts": ["reviews"],
            "gateways": ["mesh"],
            "exportTo": ["."],
            "http": [{
                "route": [
                    {"destination": {"host": "reviews", "subset": "v1", "port": {"number": 9080}}, "weight": 75},
                    {"destination": {"host": "reviews", "subset": "v2"}, "weight": 25},
                ],
            }],
        },
    }
    result = istio._parse_virtual_service(item)
    assert result.name == "reviews"
    assert result.namespace == "default"
    assert result.hosts == ["reviews"]
    assert result.export_to == ["."]
    assert len(result.destinations) == 2
    assert result.destinations[0] == istio.RouteDestination(protocol="http", host="reviews", subset="v1", port=9080, weight=75)


def test_parse_virtual_service_delegate():
    item = {
        "metadata": {"name": "root", "namespace": "default"},
        "spec": {"http": [{"delegate": {"name": "child", "namespace": "other"}}]},
    }
    result = istio._parse_virtual_service(item)
    assert result.delegates == [istio.DelegateRef(name="child", namespace="other")]


def test_parse_virtual_service_delegate_defaults_to_own_namespace():
    item = {
        "metadata": {"name": "root", "namespace": "default"},
        "spec": {"http": [{"delegate": {"name": "child"}}]},
    }
    result = istio._parse_virtual_service(item)
    assert result.delegates == [istio.DelegateRef(name="child", namespace="default")]


def test_parse_virtual_service_mirror_singular():
    item = {
        "metadata": {"name": "vs", "namespace": "default"},
        "spec": {"http": [{"mirror": {"host": "reviews-v2"}}]},
    }
    result = istio._parse_virtual_service(item)
    assert any(d.protocol == "http-mirror" and d.host == "reviews-v2" for d in result.destinations)


def test_parse_virtual_service_mirrors_plural():
    item = {
        "metadata": {"name": "vs", "namespace": "default"},
        "spec": {"http": [{"mirrors": [{"destination": {"host": "reviews-v3"}}]}]},
    }
    result = istio._parse_virtual_service(item)
    assert any(d.protocol == "http-mirror" and d.host == "reviews-v3" for d in result.destinations)


def test_parse_virtual_service_redirect_authority():
    item = {
        "metadata": {"name": "vs", "namespace": "default"},
        "spec": {"http": [{"redirect": {"authority": "new-host"}}]},
    }
    result = istio._parse_virtual_service(item)
    assert any(d.protocol == "http-redirect" and d.host == "new-host" for d in result.destinations)


def test_parse_virtual_service_tcp_and_tls_routes():
    item = {
        "metadata": {"name": "vs", "namespace": "default"},
        "spec": {
            "tcp": [{"route": [{"destination": {"host": "tcp-host"}}]}],
            "tls": [{"route": [{"destination": {"host": "tls-host"}}]}],
        },
    }
    result = istio._parse_virtual_service(item)
    protocols = {(d.protocol, d.host) for d in result.destinations}
    assert ("tcp", "tcp-host") in protocols
    assert ("tls", "tls-host") in protocols
    # mirror/delegate/redirect only apply to http routes
    assert not any(d.protocol == "http-mirror" for d in result.destinations)


def test_parse_virtual_service_ignores_route_without_host():
    item = {
        "metadata": {"name": "vs", "namespace": "default"},
        "spec": {"http": [{"route": [{"destination": {}}]}]},
    }
    result = istio._parse_virtual_service(item)
    assert result.destinations == []


# ---------------------------------------------------------------------------
# DestinationRule parsing
# ---------------------------------------------------------------------------

def test_parse_destination_rule_subsets_and_tls():
    item = {
        "metadata": {"name": "reviews", "namespace": "default"},
        "spec": {
            "host": "reviews",
            "subsets": [
                {"name": "v1", "labels": {"version": "v1"}, "trafficPolicy": {"portLevelSettings": [{"port": {"number": 9080}}]}},
            ],
            "trafficPolicy": {"tls": {"mode": "ISTIO_MUTUAL"}, "portLevelSettings": [{"port": {"number": 8080}}]},
            "exportTo": ["*"],
        },
    }
    result = istio._parse_destination_rule(item)
    assert result.host == "reviews"
    assert result.subsets == [istio.Subset(name="v1", labels={"version": "v1"})]
    assert result.tls_mode == "ISTIO_MUTUAL"
    assert result.ports == [8080, 9080]
    assert result.export_to == ["*"]


def test_parse_destination_rule_defaults():
    item = {"metadata": {"name": "x", "namespace": "default"}, "spec": {}}
    result = istio._parse_destination_rule(item)
    assert result.host == ""
    assert result.subsets == []
    assert result.tls_mode is None
    assert result.ports == []


def test_parse_destination_rule_subset_without_name_skipped():
    item = {
        "metadata": {"name": "x", "namespace": "default"},
        "spec": {"host": "x", "subsets": [{"labels": {"a": "b"}}]},
    }
    result = istio._parse_destination_rule(item)
    assert result.subsets == []


# ---------------------------------------------------------------------------
# Gateway parsing
# ---------------------------------------------------------------------------

def test_parse_gateway():
    item = {
        "metadata": {"name": "gw", "namespace": "istio-system"},
        "spec": {
            "selector": {"istio": "ingressgateway"},
            "servers": [{"hosts": ["*.example.com"], "port": {"number": 443, "protocol": "HTTPS"}, "tls": {"mode": "SIMPLE"}}],
        },
    }
    result = istio._parse_gateway(item)
    assert result.selector == {"istio": "ingressgateway"}
    assert result.servers == [istio.GatewayServer(hosts=["*.example.com"], port_number=443, protocol="HTTPS", tls_mode="SIMPLE")]


def test_gateway_direction_ingress():
    gw = istio.GatewayInfo(name="gw", namespace="istio-system", selector={"istio": "ingressgateway"})
    assert gw.direction == "ingress"


def test_gateway_direction_egress():
    gw = istio.GatewayInfo(name="gw", namespace="istio-system", selector={"istio": "egressgateway"})
    assert gw.direction == "egress"


def test_gateway_direction_custom():
    gw = istio.GatewayInfo(name="gw", namespace="default", selector={"app": "my-gw"})
    assert gw.direction == "custom"


# ---------------------------------------------------------------------------
# ServiceEntry parsing
# ---------------------------------------------------------------------------

def test_parse_service_entry():
    item = {
        "metadata": {"name": "external", "namespace": "default"},
        "spec": {
            "hosts": ["api.external.com"],
            "location": "MESH_EXTERNAL",
            "resolution": "DNS",
            "ports": [{"number": 443, "name": "https"}],
            "endpoints": [{"address": "1.2.3.4", "labels": {"a": "b"}, "ports": {"https": 443}}],
            "workloadSelector": {"labels": {"app": "external-client"}},
            "exportTo": ["."],
        },
    }
    result = istio._parse_service_entry(item)
    assert result.hosts == ["api.external.com"]
    assert result.ports == [443]
    assert result.endpoints == [istio.ServiceEntryEndpoint(address="1.2.3.4", labels={"a": "b"}, ports={"https": 443})]
    assert result.workload_selector == {"app": "external-client"}


def test_parse_service_entry_skips_endpoint_without_address():
    item = {
        "metadata": {"name": "external", "namespace": "default"},
        "spec": {"endpoints": [{"labels": {"a": "b"}}]},
    }
    result = istio._parse_service_entry(item)
    assert result.endpoints == []


# ---------------------------------------------------------------------------
# Sidecar parsing
# ---------------------------------------------------------------------------

def test_parse_sidecar():
    item = {
        "metadata": {"name": "default", "namespace": "ns1"},
        "spec": {
            "egress": [{"hosts": ["ns2/*", "istio-system/*"]}],
            "ingress": [{"port": {"number": 9080, "protocol": "HTTP"}, "defaultEndpoint": "127.0.0.1:8080"}],
            "workloadSelector": {"labels": {"app": "foo"}},
        },
    }
    result = istio._parse_sidecar(item)
    assert result.egress_hosts == ["ns2/*", "istio-system/*"]
    assert result.ingress == [istio.SidecarIngressRule(port_number=9080, protocol="HTTP", default_endpoint="127.0.0.1:8080")]
    assert result.workload_selector == {"app": "foo"}


# ---------------------------------------------------------------------------
# WorkloadEntry / WorkloadGroup parsing
# ---------------------------------------------------------------------------

def test_parse_workload_entry():
    item = {
        "metadata": {"name": "vm-1", "namespace": "default"},
        "spec": {"address": "10.0.0.1", "labels": {"app": "vm"}, "serviceAccount": "vm-sa", "ports": {"http": 8080}},
    }
    result = istio._parse_workload_entry(item)
    assert result == istio.WorkloadEntryInfo(
        name="vm-1", namespace="default", address="10.0.0.1", labels={"app": "vm"},
        service_account="vm-sa", ports={"http": 8080},
    )


def test_parse_workload_group():
    item = {
        "metadata": {"name": "vm-group", "namespace": "default"},
        "spec": {
            "metadata": {"labels": {"app": "vm"}},
            "template": {"serviceAccount": "vm-sa", "ports": {"http": 8080}},
        },
    }
    result = istio._parse_workload_group(item)
    assert result == istio.WorkloadGroupInfo(
        name="vm-group", namespace="default", labels={"app": "vm"}, service_account="vm-sa", ports={"http": 8080},
    )


# ---------------------------------------------------------------------------
# PeerAuthentication parsing
# ---------------------------------------------------------------------------

def test_parse_peer_authentication():
    item = {
        "metadata": {"name": "default", "namespace": "default"},
        "spec": {
            "mtls": {"mode": "STRICT"},
            "selector": {"matchLabels": {"app": "foo"}},
            "portLevelMtls": {8080: {"mode": "PERMISSIVE"}},
        },
    }
    result = istio._parse_peer_authentication(item)
    assert result.mtls_mode == "STRICT"
    assert result.selector == {"app": "foo"}
    assert result.port_level_mtls == {"8080": "PERMISSIVE"}


def test_parse_peer_authentication_no_mtls():
    item = {"metadata": {"name": "x", "namespace": "default"}, "spec": {}}
    result = istio._parse_peer_authentication(item)
    assert result.mtls_mode is None


# ---------------------------------------------------------------------------
# AuthorizationPolicy parsing
# ---------------------------------------------------------------------------

def test_parse_authorization_policy_rules():
    item = {
        "metadata": {"name": "allow-get", "namespace": "default"},
        "spec": {
            "action": "ALLOW",
            "selector": {"matchLabels": {"app": "httpbin"}},
            "rules": [{
                "from": [{"source": {"namespaces": ["default"], "principals": ["cluster.local/ns/default/sa/sleep"]}}],
                "to": [{"operation": {"hosts": ["httpbin.default.svc.cluster.local"]}}],
            }],
        },
    }
    result = istio._parse_authorization_policy(item)
    assert result.action == "ALLOW"
    assert result.selector == {"app": "httpbin"}
    assert result.has_selector is True
    assert result.rules[0].from_namespaces == ["default"]
    assert result.rules[0].from_principals == ["cluster.local/ns/default/sa/sleep"]
    assert result.rules[0].to_hosts == ["httpbin.default.svc.cluster.local"]


def test_parse_authorization_policy_default_action():
    item = {"metadata": {"name": "x", "namespace": "default"}, "spec": {}}
    result = istio._parse_authorization_policy(item)
    assert result.action == "ALLOW"
    assert result.has_selector is False


def test_parse_authorization_policy_single_target_ref():
    item = {
        "metadata": {"name": "x", "namespace": "default"},
        "spec": {"targetRef": {"kind": "Gateway", "name": "my-gw", "group": "gateway.networking.k8s.io"}},
    }
    result = istio._parse_authorization_policy(item)
    assert result.target_refs == [istio.TargetRef(kind="Gateway", name="my-gw", group="gateway.networking.k8s.io")]
    assert result.has_selector is True


def test_parse_authorization_policy_target_refs_list():
    item = {
        "metadata": {"name": "x", "namespace": "default"},
        "spec": {"targetRefs": [{"kind": "Service", "name": "svc-a"}, {"kind": "Service", "name": "svc-b"}]},
    }
    result = istio._parse_authorization_policy(item)
    assert [r.name for r in result.target_refs] == ["svc-a", "svc-b"]


# ---------------------------------------------------------------------------
# RequestAuthentication parsing
# ---------------------------------------------------------------------------

def test_parse_request_authentication():
    item = {
        "metadata": {"name": "jwt", "namespace": "default"},
        "spec": {
            "jwtRules": [{"issuer": "https://issuer.example.com"}],
            "selector": {"matchLabels": {"app": "httpbin"}},
        },
    }
    result = istio._parse_request_authentication(item)
    assert result.issuers == ["https://issuer.example.com"]
    assert result.selector == {"app": "httpbin"}


# ---------------------------------------------------------------------------
# get_hosts
# ---------------------------------------------------------------------------

def test_get_hosts_aggregates_and_dedups():
    resources = istio.IstioResources(
        virtual_services=[istio.VirtualServiceInfo(
            name="vs", namespace="default", hosts=["a.com"],
            destinations=[istio.RouteDestination(protocol="http", host="b.com")],
        )],
        destination_rules=[istio.DestinationRuleInfo(name="dr", namespace="default", host="a.com")],
        gateways=[istio.GatewayInfo(
            name="gw", namespace="istio-system",
            servers=[istio.GatewayServer(hosts=["c.com"], port_number=443, protocol="HTTPS", tls_mode=None)],
        )],
        service_entries=[istio.ServiceEntryInfo(name="se", namespace="default", hosts=["d.com"])],
        sidecars=[istio.SidecarInfo(name="sc", namespace="default", egress_hosts=["e.com"])],
        authorization_policies=[istio.AuthorizationPolicyInfo(
            name="ap", namespace="default", action="ALLOW",
            rules=[istio.AuthorizationRule(to_hosts=["f.com"])],
        )],
    )
    hosts = istio.get_hosts(resources)
    host_names = [h.host for h in hosts]
    assert host_names == sorted(host_names)
    assert set(host_names) == {"a.com", "b.com", "c.com", "d.com", "e.com", "f.com"}
    a_com = next(h for h in hosts if h.host == "a.com")
    assert {r.kind for r in a_com.referenced_by} == {"VirtualService.hosts", "DestinationRule.host"}


def test_get_hosts_ignores_empty_host_strings():
    resources = istio.IstioResources(
        destination_rules=[istio.DestinationRuleInfo(name="dr", namespace="default", host="")],
    )
    assert istio.get_hosts(resources) == []


# ---------------------------------------------------------------------------
# _served_version / _fetch / get_istio_resources
# ---------------------------------------------------------------------------

def test_served_version_prefers_storage_version():
    crd = V1CustomResourceDefinition(spec=V1CustomResourceDefinitionSpec(
        group="networking.istio.io", names=MagicMock(kind="VirtualService", plural="virtualservices"), scope="Namespaced",
        versions=[
            V1CustomResourceDefinitionVersion(name="v1alpha3", served=True, storage=False),
            V1CustomResourceDefinitionVersion(name="v1", served=True, storage=True),
        ],
    ))
    ext = MagicMock()
    ext.read_custom_resource_definition.return_value = crd
    assert istio._served_version(ext, "virtualservices.networking.istio.io") == "v1"


def test_served_version_falls_back_to_first_served():
    crd = V1CustomResourceDefinition(spec=V1CustomResourceDefinitionSpec(
        group="networking.istio.io", names=MagicMock(), scope="Namespaced",
        versions=[V1CustomResourceDefinitionVersion(name="v1alpha3", served=True, storage=False)],
    ))
    ext = MagicMock()
    ext.read_custom_resource_definition.return_value = crd
    assert istio._served_version(ext, "x") == "v1alpha3"


def test_served_version_returns_none_when_crd_missing():
    ext = MagicMock()
    ext.read_custom_resource_definition.side_effect = ApiException(status=404)
    assert istio._served_version(ext, "missing.example.io") is None


def test_fetch_returns_empty_when_crd_not_served(monkeypatch):
    monkeypatch.setattr(istio, "_served_version", lambda ext, name: None)
    result = istio._fetch(MagicMock(), MagicMock(), group="g", plural="p", namespace=None, parser=lambda x: x)
    assert result == []


def test_fetch_parses_items(monkeypatch):
    monkeypatch.setattr(istio, "_served_version", lambda ext, name: "v1")
    monkeypatch.setattr(istio, "_custom_list", lambda custom, *, group, version, namespace, plural: {
        "items": [{"metadata": {"name": "a", "namespace": "default"}}],
    })
    result = istio._fetch(MagicMock(), MagicMock(), group="g", plural="p", namespace=None, parser=istio._parse_sidecar)
    assert result == [istio.SidecarInfo(name="a", namespace="default")]


def test_fetch_returns_empty_on_list_error(monkeypatch):
    monkeypatch.setattr(istio, "_served_version", lambda ext, name: "v1")

    def raise_error(*args, **kwargs):
        raise ApiException(status=403)

    monkeypatch.setattr(istio, "_custom_list", raise_error)
    result = istio._fetch(MagicMock(), MagicMock(), group="g", plural="p", namespace=None, parser=lambda x: x)
    assert result == []


def test_get_istio_resources_aggregates_all_types(monkeypatch):
    def fake_fetch(ext, custom, *, group, plural, namespace, parser):
        return [f"{plural}-item"]

    monkeypatch.setattr(istio, "_fetch", fake_fetch)
    result = istio.get_istio_resources(namespace="default")
    assert result.virtual_services == ["virtualservices-item"]
    assert result.destination_rules == ["destinationrules-item"]
    assert result.gateways == ["gateways-item"]
    assert result.service_entries == ["serviceentries-item"]
    assert result.sidecars == ["sidecars-item"]
    assert result.workload_entries == ["workloadentries-item"]
    assert result.workload_groups == ["workloadgroups-item"]
    assert result.peer_authentications == ["peerauthentications-item"]
    assert result.authorization_policies == ["authorizationpolicies-item"]
    assert result.request_authentications == ["requestauthentications-item"]
