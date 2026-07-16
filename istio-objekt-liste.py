"""CLI: dump every collected Kubernetes/Istio object as one flat JSON document."""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys

import urllib3
from kubernetes.client.rest import ApiException
from kubernetes.config import ConfigException

from istio import get_istio_resources
from kubectl import (
    get_mesh_root_namespace,
    get_namespaces,
    get_pods,
    get_service_accounts,
    get_services,
    load_config,
)


def _collect(namespace: str | None) -> dict[str, object]:
    resources = get_istio_resources(namespace=namespace)
    return {
        "mesh_root_namespace": get_mesh_root_namespace(),
        "namespaces": [dataclasses.asdict(n) for n in get_namespaces()],
        "services": [dataclasses.asdict(s) for s in get_services(namespace=namespace)],
        "service_accounts": [dataclasses.asdict(sa) for sa in get_service_accounts(namespace=namespace)],
        "pods": [dataclasses.asdict(p) for p in get_pods(namespace=namespace)],
        "virtual_services": [dataclasses.asdict(o) for o in resources.virtual_services],
        "destination_rules": [dataclasses.asdict(o) for o in resources.destination_rules],
        "gateways": [dataclasses.asdict(o) for o in resources.gateways],
        "service_entries": [dataclasses.asdict(o) for o in resources.service_entries],
        "sidecars": [dataclasses.asdict(o) for o in resources.sidecars],
        "workload_entries": [dataclasses.asdict(o) for o in resources.workload_entries],
        "workload_groups": [dataclasses.asdict(o) for o in resources.workload_groups],
        "peer_authentications": [dataclasses.asdict(o) for o in resources.peer_authentications],
        "authorization_policies": [dataclasses.asdict(o) for o in resources.authorization_policies],
        "request_authentications": [dataclasses.asdict(o) for o in resources.request_authentications],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump every collected Kubernetes/Istio object (namespaces, services, "
                    "service accounts, pods, and all Istio CRDs) as one flat JSON document.",
    )
    parser.add_argument(
        "-n", "--namespace",
        default=None,
        help="Only collect from this namespace (default: every namespace in the cluster).",
    )
    parser.add_argument(
        "--insecure-skip-tls-verify",
        action="store_true",
        help="Disable TLS certificate verification against the API server "
             "(equivalent to kubectl/oc --insecure-skip-tls-verify).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging, e.g. for API calls that were skipped due to errors.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
    )

    try:
        load_config(verify_ssl=not args.insecure_skip_tls_verify)
    except ConfigException as e:
        print(f"Error: could not load Kubernetes configuration: {e}", file=sys.stderr)
        return 1

    try:
        data = _collect(args.namespace)
    except (ApiException, urllib3.exceptions.HTTPError) as e:
        print(f"Error: could not reach the Kubernetes API server: {e}", file=sys.stderr)
        return 1

    print(json.dumps(data, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
