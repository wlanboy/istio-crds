from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def sj(load_module):
    return load_module("sync_job", "sync-job.py")


# ---------------------------------------------------------------------------
# _env_flag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "Yes"])
def test_env_flag_true_values(sj, monkeypatch, value):
    monkeypatch.setenv("FLAG", value)
    assert sj._env_flag("FLAG") is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
def test_env_flag_false_values(sj, monkeypatch, value):
    monkeypatch.setenv("FLAG", value)
    assert sj._env_flag("FLAG") is False


def test_env_flag_missing_variable_is_false(sj, monkeypatch):
    monkeypatch.delenv("FLAG", raising=False)
    assert sj._env_flag("FLAG") is False


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------

def test_run_success_invokes_both_scripts_with_expected_args(sj, tmp_path, monkeypatch):
    graph_output = tmp_path / "out" / "graph.json"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return MagicMock(returncode=0)

    monkeypatch.setattr(sj.subprocess, "run", fake_run)
    ok = sj.run(
        graph_output=graph_output, namespace="default", insecure_skip_tls_verify=True,
        datenimport_clear=True, verbose=True,
    )
    assert ok is True
    assert graph_output.parent.is_dir()
    assert len(calls) == 2

    graph_cmd, graph_kwargs = calls[0]
    assert graph_cmd[0] == sys.executable
    assert graph_cmd[1] == str(sj.BASE_DIR / "istio-graph.py")
    assert "-n" in graph_cmd and "default" in graph_cmd
    assert "--insecure-skip-tls-verify" in graph_cmd
    assert "-v" in graph_cmd
    assert graph_kwargs["cwd"] == sj.BASE_DIR

    import_cmd, import_kwargs = calls[1]
    assert import_cmd[0] == sys.executable
    assert import_cmd[1] == str(sj.BASE_DIR / "datenimport.py")
    assert str(graph_output) in import_cmd
    assert "--clear" in import_cmd
    assert "-v" in import_cmd


def test_run_omits_optional_flags_when_disabled(sj, tmp_path, monkeypatch):
    graph_output = tmp_path / "graph.json"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0)

    monkeypatch.setattr(sj.subprocess, "run", fake_run)
    sj.run(graph_output=graph_output, namespace=None, insecure_skip_tls_verify=False, datenimport_clear=False, verbose=False)
    graph_cmd, import_cmd = calls
    assert "-n" not in graph_cmd
    assert "--insecure-skip-tls-verify" not in graph_cmd
    assert "-v" not in graph_cmd
    assert "--clear" not in import_cmd
    assert "-v" not in import_cmd


def test_run_stops_after_graph_failure(sj, tmp_path, monkeypatch):
    graph_output = tmp_path / "graph.json"
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=1)

    monkeypatch.setattr(sj.subprocess, "run", fake_run)
    ok = sj.run(graph_output=graph_output, namespace=None, insecure_skip_tls_verify=False, datenimport_clear=False, verbose=False)
    assert ok is False
    assert len(calls) == 1


def test_run_reports_failure_when_import_fails(sj, tmp_path, monkeypatch):
    graph_output = tmp_path / "graph.json"
    results = iter([MagicMock(returncode=0), MagicMock(returncode=1)])

    monkeypatch.setattr(sj.subprocess, "run", lambda cmd, **kwargs: next(results))
    ok = sj.run(graph_output=graph_output, namespace=None, insecure_skip_tls_verify=False, datenimport_clear=False, verbose=False)
    assert ok is False


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def test_main_wires_env_vars_into_run(sj, monkeypatch, tmp_path):
    captured = {}

    def fake_run(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(sj, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["sync-job.py"])
    monkeypatch.setenv("ISTIO_NAMESPACE", "default")
    monkeypatch.setenv("ISTIO_INSECURE_SKIP_TLS_VERIFY", "true")
    monkeypatch.setenv("DATENIMPORT_CLEAR", "1")
    monkeypatch.setenv("VERBOSE", "yes")
    monkeypatch.setenv("GRAPH_OUTPUT", "custom/graph.json")

    exit_code = sj.main()

    assert exit_code == 0
    assert captured["namespace"] == "default"
    assert captured["insecure_skip_tls_verify"] is True
    assert captured["datenimport_clear"] is True
    assert captured["verbose"] is True
    assert captured["graph_output"] == sj.BASE_DIR / "custom/graph.json"


def test_main_returns_1_when_run_fails(sj, monkeypatch):
    monkeypatch.setattr(sj, "run", lambda **kwargs: False)
    monkeypatch.setattr(sys, "argv", ["sync-job.py"])
    for var in ("ISTIO_NAMESPACE", "ISTIO_INSECURE_SKIP_TLS_VERIFY", "DATENIMPORT_CLEAR", "VERBOSE", "GRAPH_OUTPUT"):
        monkeypatch.delenv(var, raising=False)
    assert sj.main() == 1


def test_main_keeps_absolute_graph_output_untouched(sj, monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(sj, "run", lambda **kwargs: captured.update(kwargs) or True)
    absolute_path = str(tmp_path / "graph.json")
    monkeypatch.setattr(sys, "argv", ["sync-job.py", "--output", absolute_path])
    for var in ("ISTIO_NAMESPACE", "ISTIO_INSECURE_SKIP_TLS_VERIFY", "DATENIMPORT_CLEAR", "VERBOSE", "GRAPH_OUTPUT"):
        monkeypatch.delenv(var, raising=False)
    sj.main()
    assert captured["graph_output"] == Path(absolute_path)
