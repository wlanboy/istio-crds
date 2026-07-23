from __future__ import annotations

import hashlib
import io
import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def da(load_module):
    return load_module("datenimport_arangodb", "arangodb/datenimport-arangodb.py")


# ---------------------------------------------------------------------------
# _node_key / _attrs_key
# ---------------------------------------------------------------------------

def test_node_key_is_sha1_of_id(da):
    node_id = "service:default/httpbin"
    assert da._node_key(node_id) == hashlib.sha1(node_id.encode()).hexdigest()


def test_node_key_differs_for_different_ids(da):
    assert da._node_key("a") != da._node_key("b")


def test_attrs_key_deterministic_and_order_independent(da):
    assert da._attrs_key({"a": 1, "b": 2}) == da._attrs_key({"b": 2, "a": 1})


def test_attrs_key_differs_for_different_content(da):
    assert da._attrs_key({"a": 1}) != da._attrs_key({"a": 2})


# ---------------------------------------------------------------------------
# _load_graph
# ---------------------------------------------------------------------------

def test_load_graph_from_file(da, tmp_path):
    graph = {"nodes": [], "edges": []}
    path = tmp_path / "graph.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    assert da._load_graph(str(path)) == graph


def test_load_graph_from_stdin(da, monkeypatch):
    graph = {"nodes": [{"id": "a"}], "edges": []}
    monkeypatch.setattr(da.sys, "stdin", io.StringIO(json.dumps(graph)))
    assert da._load_graph("-") == graph


# ---------------------------------------------------------------------------
# _import_nodes / _import_edges
# ---------------------------------------------------------------------------

def test_import_nodes_builds_expected_rows(da):
    db = MagicMock()
    nodes = [{
        "id": "service:default/httpbin", "kind": "service", "name": "httpbin", "namespace": "default",
        "attributes": {"ports": [80]},
    }]
    da._import_nodes(db, nodes)
    db.aql.execute.assert_called_once()
    _, kwargs = db.aql.execute.call_args
    rows = kwargs["bind_vars"]["rows"]
    assert rows == [{
        "_key": da._node_key("service:default/httpbin"),
        "id": "service:default/httpbin",
        "kind": "service",
        "name": "httpbin",
        "namespace": "default",
        "ports": [80],
    }]


def test_import_edges_builds_from_to_and_attrs_key(da):
    db = MagicMock()
    edges = [{"source": "a", "target": "b", "relation": "routes_to", "attributes": {"weight": 75}}]
    da._import_edges(db, edges)
    _, kwargs = db.aql.execute.call_args
    rows = kwargs["bind_vars"]["rows"]
    assert rows == [{
        "_key": da._attrs_key({"source": "a", "target": "b", "relation": "routes_to", "weight": 75}),
        "_from": f"nodes/{da._node_key('a')}",
        "_to": f"nodes/{da._node_key('b')}",
        "relation": "routes_to",
        "weight": 75,
    }]


# ---------------------------------------------------------------------------
# import_graph
# ---------------------------------------------------------------------------

def test_import_graph_returns_counts(da):
    db = MagicMock()
    graph = {
        "nodes": [{"id": "n1", "kind": "service", "name": "a", "namespace": "default"}],
        "edges": [{"source": "n1", "target": "n1", "relation": "self_loop"}],
    }
    node_count, edge_count = da.import_graph(db, graph, clear=False)
    assert (node_count, edge_count) == (1, 1)
    db.collection.assert_not_called()


def test_import_graph_clear_truncates_collections(da):
    db = MagicMock()
    da.import_graph(db, {"nodes": [], "edges": []}, clear=True)
    db.collection.assert_any_call("nodes")
    db.collection.assert_any_call("edges")
    db.collection.return_value.truncate.assert_called()
