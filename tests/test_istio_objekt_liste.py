from __future__ import annotations

import sys

import pytest
from kubernetes.client.rest import ApiException
from kubernetes.config import ConfigException

import kubectl
from istio import IstioResources, SidecarInfo


@pytest.fixture
def iol(load_module):
    return load_module("istio_objekt_liste", "istio-objekt-liste.py")


def test_collect_assembles_all_sections(iol, monkeypatch):
    resources = IstioResources(sidecars=[SidecarInfo(name="default", namespace="ns1")])
    namespaces = [kubectl.NamespaceInfo(name="ns1")]

    monkeypatch.setattr(iol, "get_istio_resources", lambda namespace=None: resources)
    monkeypatch.setattr(iol, "get_hosts", lambda r: [])
    monkeypatch.setattr(iol, "get_mesh_root_namespace", lambda: "istio-system")
    monkeypatch.setattr(iol, "get_namespaces", lambda namespace=None: namespaces)
    monkeypatch.setattr(iol, "get_services", lambda namespace=None: [])
    monkeypatch.setattr(iol, "get_service_accounts", lambda namespace=None: [])
    monkeypatch.setattr(iol, "get_pods", lambda namespace=None: [])
    monkeypatch.setattr(iol, "get_network_policies", lambda namespace=None: [])

    data = iol._collect(namespace=None)

    assert data["mesh_root_namespace"] == "istio-system"
    assert data["namespaces"] == [{"name": "ns1", "labels": {}}]
    assert data["sidecars"] == [{"name": "default", "namespace": "ns1", "egress_hosts": [], "ingress": [], "workload_selector": {}}]
    assert data["services"] == []
    expected_keys = {
        "mesh_root_namespace", "namespaces", "services", "service_accounts", "pods", "network_policies",
        "hosts", "virtual_services", "destination_rules", "gateways", "service_entries", "sidecars",
        "workload_entries", "workload_groups", "peer_authentications", "authorization_policies",
        "request_authentications",
    }
    assert set(data.keys()) == expected_keys


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def test_main_returns_1_on_config_error(iol, monkeypatch, capsys):
    monkeypatch.setattr(iol, "load_config", lambda verify_ssl=True: (_ for _ in ()).throw(ConfigException("bad config")))
    monkeypatch.setattr(sys, "argv", ["istio-objekt-liste.py"])
    assert iol.main() == 1
    assert "could not load Kubernetes configuration" in capsys.readouterr().err


def test_main_returns_1_on_api_error(iol, monkeypatch, capsys):
    monkeypatch.setattr(iol, "load_config", lambda verify_ssl=True: None)

    def raise_api_error(namespace):
        raise ApiException(status=500)

    monkeypatch.setattr(iol, "_collect", raise_api_error)
    monkeypatch.setattr(sys, "argv", ["istio-objekt-liste.py"])
    assert iol.main() == 1
    assert "could not reach the Kubernetes API server" in capsys.readouterr().err


def test_main_prints_json_on_success(iol, monkeypatch, capsys):
    monkeypatch.setattr(iol, "load_config", lambda verify_ssl=True: None)
    monkeypatch.setattr(iol, "_collect", lambda namespace: {"mesh_root_namespace": "istio-system"})
    monkeypatch.setattr(sys, "argv", ["istio-objekt-liste.py"])
    assert iol.main() == 0
    out = capsys.readouterr().out
    assert '"mesh_root_namespace": "istio-system"' in out


def test_main_passes_namespace_argument_through(iol, monkeypatch):
    monkeypatch.setattr(iol, "load_config", lambda verify_ssl=True: None)
    captured = {}
    monkeypatch.setattr(iol, "_collect", lambda namespace: captured.setdefault("namespace", namespace) or {})
    monkeypatch.setattr(sys, "argv", ["istio-objekt-liste.py", "-n", "default"])
    iol.main()
    assert captured["namespace"] == "default"
