"""CLI: list Istio CRDs with all versions."""
from __future__ import annotations

import argparse
import logging
import sys

import urllib3
from kubernetes.client.rest import ApiException
from kubernetes.config import ConfigException

from kubectl import CRDVersionedInfo, get_crd_versions, load_config


def _format_table(rows: list[tuple[str, ...]], headers: tuple[str, ...]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt_row(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    lines = [fmt_row(headers), fmt_row(tuple("-" * w for w in widths))]
    lines.extend(fmt_row(row) for row in rows)
    return "\n".join(lines)


def _print_deprecation_warnings(crds: list[CRDVersionedInfo]) -> None:
    lines = [
        f"  {crd.name} {v.version}: {v.deprecation_warning or 'no deprecation message provided'}"
        for crd in crds
        for v in crd.versions
        if v.deprecated
    ]
    if lines:
        print("\nDeprecated API versions:")
        print("\n".join(lines))


def _print_unhealthy_crds(crds: list[CRDVersionedInfo]) -> None:
    lines = []
    for crd in crds:
        if not crd.established:
            lines.append(
                f"  {crd.name}: not Established "
                f"({crd.established_message or 'no message provided'})",
            )
        if not crd.names_accepted:
            lines.append(
                f"  {crd.name}: NamesAccepted=False "
                f"({crd.names_accepted_message or 'no message provided'})",
            )
    if lines:
        print("\nUnhealthy CRDs (status conditions):")
        print("\n".join(lines))


def _print_migration_candidates(crds: list[CRDVersionedInfo]) -> None:
    lines = [
        f"  {crd.name}: instances still stored as {crd.pending_migration_versions} "
        f"(current storage version: {crd.storage_version})"
        for crd in crds
        if crd.pending_migration_versions
    ]
    if lines:
        print("\nStorage version migration candidates (status.storedVersions not yet cleaned up):")
        print("\n".join(lines))


def _is_istio_crd(crd: CRDVersionedInfo) -> bool:
    return crd.group == "istio.io" or crd.group.endswith(".istio.io")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="List all Istio CRDs (group *.istio.io) in a Kubernetes cluster "
                    "with every API version.",
    )
    parser.add_argument(
        "-n", "--namespace",
        default=None,
        help="Only show namespaced CRDs (cluster-scoped CRDs are only shown "
             "when this is omitted).",
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
        crds = get_crd_versions(namespace=args.namespace)
    except (ApiException, urllib3.exceptions.HTTPError) as e:
        print(f"Error: could not reach the Kubernetes API server: {e}", file=sys.stderr)
        return 1

    crds = [crd for crd in crds if _is_istio_crd(crd)]

    if not crds:
        print("No Istio CRDs found.")
        return 0

    rows = [
        (
            crd.name,
            crd.group,
            crd.kind,
            "Namespaced" if crd.namespaced else "Cluster",
            crd.conversion_strategy,
            v.version,
            "yes" if v.served else "no",
            "yes" if v.storage else "no",
            "yes" if v.deprecated else "no",
        )
        for crd in crds
        for v in crd.versions
    ]

    headers = ("CRD", "GROUP", "KIND", "SCOPE", "CONVERSION", "VERSION", "SERVED", "STORAGE", "DEPRECATED")
    print(_format_table(rows, headers))
    _print_deprecation_warnings(crds)
    _print_migration_candidates(crds)
    _print_unhealthy_crds(crds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
