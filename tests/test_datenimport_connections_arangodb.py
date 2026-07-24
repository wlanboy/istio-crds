from __future__ import annotations

import hashlib
import io
import json
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def dca(load_module):
    return load_module("datenimport_connections_arangodb", "arangodb/datenimport-connections-arangodb.py")


# ---------------------------------------------------------------------------
# _node_key / _attrs_key
# ---------------------------------------------------------------------------

def test_node_key_is_sha1_of_id(dca):
    node_id = "deployment:default/httpbin"
    assert dca._node_key(node_id) == hashlib.sha1(node_id.encode()).hexdigest()


def test_node_key_differs_for_different_ids(dca):
    assert dca._node_key("a") != dca._node_key("b")


def test_attrs_key_deterministic_and_order_independent(dca):
    assert dca._attrs_key({"a": 1, "b": 2}) == dca._attrs_key({"b": 2, "a": 1})


def test_attrs_key_differs_for_different_content(dca):
    assert dca._attrs_key({"a": 1}) != dca._attrs_key({"a": 2})


# ---------------------------------------------------------------------------
# _load_graph
# ---------------------------------------------------------------------------

def test_load_graph_from_file(dca, tmp_path):
    graph = {"nodes": [], "edges": []}
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(graph), encoding="utf-8")
    assert dca._load_graph(str(path)) == graph


def test_load_graph_from_stdin(dca, monkeypatch):
    graph = {"nodes": [{"id": "a"}], "edges": []}
    monkeypatch.setattr(dca.sys, "stdin", io.StringIO(json.dumps(graph)))
    assert dca._load_graph("-") == graph


# ---------------------------------------------------------------------------
# _import_nodes / _import_edges
# ---------------------------------------------------------------------------

def test_import_nodes_builds_expected_rows(dca):
    db = MagicMock()
    nodes = [{
        "id": "deployment:default/httpbin", "kind": "deployment", "name": "httpbin", "namespace": "default",
        "attributes": {"service_account": "httpbin"},
    }]
    dca._import_nodes(db, nodes)
    db.aql.execute.assert_called_once()
    _, kwargs = db.aql.execute.call_args
    rows = kwargs["bind_vars"]["rows"]
    assert rows == [{
        "_key": dca._node_key("deployment:default/httpbin"),
        "id": "deployment:default/httpbin",
        "kind": "deployment",
        "name": "httpbin",
        "namespace": "default",
        "service_account": "httpbin",
    }]


def test_import_edges_builds_from_to_and_attrs_key(dca):
    db = MagicMock()
    edges = [{
        "source": "deployment:default/sleep", "target": "deployment:default/httpbin",
        "relation": "forbidden", "attributes": {},
    }]
    dca._import_edges(db, edges)
    _, kwargs = db.aql.execute.call_args
    rows = kwargs["bind_vars"]["rows"]
    assert rows == [{
        "_key": dca._attrs_key({
            "source": "deployment:default/sleep", "target": "deployment:default/httpbin", "relation": "forbidden",
        }),
        "_from": f"nodes/{dca._node_key('deployment:default/sleep')}",
        "_to": f"nodes/{dca._node_key('deployment:default/httpbin')}",
        "relation": "forbidden",
    }]


# ---------------------------------------------------------------------------
# import_graph
# ---------------------------------------------------------------------------

def test_import_graph_returns_counts(dca):
    db = MagicMock()
    graph = {
        "nodes": [{"id": "n1", "kind": "deployment", "name": "a", "namespace": "default"}],
        "edges": [{"source": "n1", "target": "n1", "relation": "self_loop"}],
    }
    node_count, edge_count = dca.import_graph(db, graph, clear=False)
    assert (node_count, edge_count) == (1, 1)
    db.collection.assert_not_called()


def test_import_graph_clear_truncates_collections(dca):
    db = MagicMock()
    dca.import_graph(db, {"nodes": [], "edges": []}, clear=True)
    db.collection.assert_any_call("nodes")
    db.collection.assert_any_call("edges")
    db.collection.return_value.truncate.assert_called()
