from __future__ import annotations

import io
import json
from unittest.mock import MagicMock, call

import pytest

import datenimport


# ---------------------------------------------------------------------------
# _sanitize_identifier
# ---------------------------------------------------------------------------

def test_sanitize_identifier_accepts_valid_name():
    assert datenimport._sanitize_identifier("VirtualService", kind="Label") == "VirtualService"


@pytest.mark.parametrize("value", ["Foo-Bar", "1Foo", "Foo Bar", "Foo`; DROP TABLE", ""])
def test_sanitize_identifier_rejects_invalid_names(value):
    with pytest.raises(ValueError, match="Ungültiger"):
        datenimport._sanitize_identifier(value, kind="Label")


# ---------------------------------------------------------------------------
# _to_label
# ---------------------------------------------------------------------------

def test_to_label_capitalizes_first_letter_only():
    assert datenimport._to_label("virtualService") == "VirtualService"


# ---------------------------------------------------------------------------
# _to_property_value / _flatten_attributes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [None, "text", 42, 3.14, True, False])
def test_to_property_value_passes_through_primitives(value):
    assert datenimport._to_property_value(value) is value


def test_to_property_value_passes_through_homogeneous_list():
    value = [1, 2, 3]
    assert datenimport._to_property_value(value) == [1, 2, 3]


def test_to_property_value_serializes_nested_dict():
    result = datenimport._to_property_value({"a": 1})
    assert result == json.dumps({"a": 1}, sort_keys=True)


def test_to_property_value_serializes_list_of_dicts():
    value = [{"port": 80}, {"port": 443}]
    result = datenimport._to_property_value(value)
    assert result == json.dumps(value, sort_keys=True)


def test_flatten_attributes_mixes_primitive_and_serialized_values():
    result = datenimport._flatten_attributes({"name": "httpbin", "selector": {"app": "httpbin"}})
    assert result == {"name": "httpbin", "selector": json.dumps({"app": "httpbin"}, sort_keys=True)}


# ---------------------------------------------------------------------------
# _attrs_key
# ---------------------------------------------------------------------------

def test_attrs_key_is_deterministic():
    assert datenimport._attrs_key({"a": 1, "b": 2}) == datenimport._attrs_key({"a": 1, "b": 2})


def test_attrs_key_independent_of_key_order():
    assert datenimport._attrs_key({"a": 1, "b": 2}) == datenimport._attrs_key({"b": 2, "a": 1})


def test_attrs_key_differs_for_different_content():
    assert datenimport._attrs_key({"a": 1}) != datenimport._attrs_key({"a": 2})


# ---------------------------------------------------------------------------
# _load_graph
# ---------------------------------------------------------------------------

def test_load_graph_from_file(tmp_path):
    graph = {"nodes": [], "edges": []}
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    assert datenimport._load_graph(str(path)) == graph


def test_load_graph_from_stdin(monkeypatch):
    graph = {"nodes": [{"id": "a"}], "edges": []}
    monkeypatch.setattr(datenimport.sys, "stdin", io.StringIO(json.dumps(graph)))
    assert datenimport._load_graph("-") == graph


# ---------------------------------------------------------------------------
# _clear_database / _ensure_constraints / _import_nodes / _import_edges
# ---------------------------------------------------------------------------

def test_clear_database_runs_detach_delete():
    session = MagicMock()
    datenimport._clear_database(session)
    session.run.assert_called_once_with("MATCH (n) DETACH DELETE n")


def test_ensure_constraints_creates_one_per_kind():
    session = MagicMock()
    nodes = [{"kind": "service"}, {"kind": "pod"}, {"kind": "service"}]
    datenimport._ensure_constraints(session, nodes)
    assert session.run.call_count == 2
    queries = {c.args[0] for c in session.run.call_args_list}
    assert any("`Service`" in q for q in queries)
    assert any("`Pod`" in q for q in queries)


def test_ensure_constraints_rejects_malicious_kind():
    session = MagicMock()
    with pytest.raises(ValueError):
        datenimport._ensure_constraints(session, [{"kind": "service`) DETACH DELETE"}])


def test_import_nodes_groups_by_kind_and_builds_rows():
    session = MagicMock()
    nodes = [
        {"id": "service:default/httpbin", "kind": "service", "name": "httpbin", "namespace": "default", "attributes": {"ports": [80]}},
        {"id": "pod:default/httpbin-1", "kind": "pod", "name": "httpbin-1", "namespace": "default"},
    ]
    datenimport._import_nodes(session, nodes)
    assert session.run.call_count == 2
    calls_by_label = {c.kwargs["rows"][0]["kind"]: c for c in session.run.call_args_list}
    service_call = calls_by_label["service"]
    assert "`Service`" in service_call.args[0]
    assert service_call.kwargs["rows"][0]["props"] == {"ports": [80]}
    pod_call = calls_by_label["pod"]
    assert pod_call.kwargs["rows"][0]["props"] == {}


def test_import_edges_groups_by_relation_and_includes_attrs_key():
    session = MagicMock()
    edges = [
        {"source": "a", "target": "b", "relation": "routes_to", "attributes": {"weight": 75}},
        {"source": "a", "target": "c", "relation": "routes_to", "attributes": {"weight": 25}},
    ]
    datenimport._import_edges(session, edges)
    assert session.run.call_count == 1
    query, kwargs = session.run.call_args
    assert "`ROUTES_TO`" in query[0]
    rows = kwargs["rows"]
    assert len(rows) == 2
    assert rows[0]["attrs_key"] != rows[1]["attrs_key"]


def test_import_edges_rejects_malicious_relation():
    session = MagicMock()
    edges = [{"source": "a", "target": "b", "relation": "foo`]-() MATCH (n"}]
    with pytest.raises(ValueError):
        datenimport._import_edges(session, edges)


# ---------------------------------------------------------------------------
# import_graph
# ---------------------------------------------------------------------------

def _mock_driver():
    session = MagicMock()
    driver = MagicMock()
    driver.session.return_value.__enter__.return_value = session
    driver.session.return_value.__exit__.return_value = False
    return driver, session


def test_import_graph_returns_counts_and_uses_correct_database():
    driver, session = _mock_driver()
    graph = {
        "nodes": [{"id": "n1", "kind": "service", "name": "a", "namespace": "default", "attributes": {}}],
        "edges": [{"source": "n1", "target": "n1", "relation": "self_loop", "attributes": {}}],
    }
    node_count, edge_count = datenimport.import_graph(driver, "neo4j", graph, clear=False)
    assert (node_count, edge_count) == (1, 1)
    driver.session.assert_called_once_with(database="neo4j")


def test_import_graph_clear_true_deletes_first():
    driver, session = _mock_driver()
    datenimport.import_graph(driver, "neo4j", {"nodes": [], "edges": []}, clear=True)
    assert session.run.call_args_list[0] == call("MATCH (n) DETACH DELETE n")


def test_import_graph_clear_false_skips_delete():
    driver, session = _mock_driver()
    datenimport.import_graph(driver, "neo4j", {"nodes": [], "edges": []}, clear=False)
    assert all(c != call("MATCH (n) DETACH DELETE n") for c in session.run.call_args_list)
