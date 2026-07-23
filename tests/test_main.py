from __future__ import annotations

import sys

import pytest
from kubernetes.client.rest import ApiException
from kubernetes.config import ConfigException

import kubectl
import main


def _crd(name, group="networking.istio.io", kind="VirtualService", versions=None, **kwargs):
    return kubectl.CRDVersionedInfo(
        name=name, group=group, kind=kind, plural=name.split(".")[0], namespaced=True,
        versions=versions or [kubectl.CRDVersionInfo(version="v1", served=True, storage=True)],
        **kwargs,
    )


# ---------------------------------------------------------------------------
# _format_table
# ---------------------------------------------------------------------------

def test_format_table_aligns_columns():
    rows = [("a", "bb"), ("ccc", "d")]
    headers = ("H1", "H2")
    table = main._format_table(rows, headers)
    lines = table.splitlines()
    assert lines[0].startswith("H1 ")
    assert lines[3].startswith("ccc")
    # every data row/header line should have the same width as the separator line
    assert len(lines[1]) == len(lines[0])


def test_format_table_widens_to_fit_longest_cell():
    rows = [("very-long-crd-name", "x")]
    headers = ("CRD", "OTHER")
    table = main._format_table(rows, headers)
    header_line, sep_line, data_line = table.splitlines()
    assert data_line.startswith("very-long-crd-name")
    assert header_line.startswith("CRD" + " " * (len("very-long-crd-name") - len("CRD") + 2))


# ---------------------------------------------------------------------------
# _is_istio_crd
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("group,expected", [
    ("istio.io", True),
    ("networking.istio.io", True),
    ("security.istio.io", True),
    ("cert-manager.io", False),
    ("example.com", False),
])
def test_is_istio_crd(group, expected):
    assert main._is_istio_crd(_crd("x.example", group=group)) is expected


# ---------------------------------------------------------------------------
# _print_deprecation_warnings / _print_unhealthy_crds / _print_migration_candidates
# ---------------------------------------------------------------------------

def test_print_deprecation_warnings_lists_deprecated_versions(capsys):
    crds = [_crd("vs.networking.istio.io", versions=[
        kubectl.CRDVersionInfo(version="v1alpha3", served=True, storage=False, deprecated=True, deprecation_warning="use v1"),
        kubectl.CRDVersionInfo(version="v1", served=True, storage=True),
    ])]
    main._print_deprecation_warnings(crds)
    out = capsys.readouterr().out
    assert "Deprecated API versions:" in out
    assert "v1alpha3: use v1" in out
    assert "v1:" not in out.split("Deprecated API versions:")[1]


def test_print_deprecation_warnings_silent_when_none_deprecated(capsys):
    crds = [_crd("vs.networking.istio.io")]
    main._print_deprecation_warnings(crds)
    assert capsys.readouterr().out == ""


def test_print_unhealthy_crds_reports_established_and_names_accepted(capsys):
    crds = [_crd("broken.example.io", established=False, established_message="conflict", names_accepted=False, names_accepted_message="dup")]
    main._print_unhealthy_crds(crds)
    out = capsys.readouterr().out
    assert "not Established" in out
    assert "conflict" in out
    assert "NamesAccepted=False" in out
    assert "dup" in out


def test_print_unhealthy_crds_silent_when_healthy(capsys):
    main._print_unhealthy_crds([_crd("healthy.example.io")])
    assert capsys.readouterr().out == ""


def test_print_migration_candidates_lists_pending_versions(capsys):
    crd = _crd("vs.networking.istio.io", versions=[kubectl.CRDVersionInfo(version="v1", served=True, storage=True)],
                stored_versions=["v1alpha3", "v1"])
    main._print_migration_candidates([crd])
    out = capsys.readouterr().out
    assert "storage version migration candidates" in out.lower()
    assert "v1alpha3" in out


def test_print_migration_candidates_silent_when_fully_migrated(capsys):
    crd = _crd("vs.networking.istio.io", versions=[kubectl.CRDVersionInfo(version="v1", served=True, storage=True)],
                stored_versions=["v1"])
    main._print_migration_candidates([crd])
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def test_main_returns_1_on_config_error(monkeypatch, capsys):
    monkeypatch.setattr(main, "load_config", lambda verify_ssl=True: (_ for _ in ()).throw(ConfigException("no config")))
    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert main.main() == 1
    assert "could not load Kubernetes configuration" in capsys.readouterr().err


def test_main_returns_1_on_api_error(monkeypatch, capsys):
    monkeypatch.setattr(main, "load_config", lambda verify_ssl=True: None)

    def raise_api_error(namespace=None):
        raise ApiException(status=500)

    monkeypatch.setattr(main, "get_crd_versions", raise_api_error)
    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert main.main() == 1
    assert "could not reach the Kubernetes API server" in capsys.readouterr().err


def test_main_reports_no_istio_crds_found(monkeypatch, capsys):
    monkeypatch.setattr(main, "load_config", lambda verify_ssl=True: None)
    monkeypatch.setattr(main, "get_crd_versions", lambda namespace=None: [_crd("x.cert-manager.io", group="cert-manager.io")])
    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert main.main() == 0
    assert "No Istio CRDs found." in capsys.readouterr().out


def test_main_prints_table_for_istio_crds(monkeypatch, capsys):
    monkeypatch.setattr(main, "load_config", lambda verify_ssl=True: None)
    monkeypatch.setattr(main, "get_crd_versions", lambda namespace=None: [_crd("virtualservices.networking.istio.io")])
    monkeypatch.setattr(sys, "argv", ["main.py"])
    assert main.main() == 0
    out = capsys.readouterr().out
    assert "virtualservices.networking.istio.io" in out
    assert "CRD" in out and "GROUP" in out
