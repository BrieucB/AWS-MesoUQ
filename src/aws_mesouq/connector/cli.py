"""Session connector CLI; no deployment or unattended credential persistence."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
import time

from .api import API
from .auth import browser_login
from .connector import Connector
from .journal import Journal
from .slurm import Slurm
from .transport import Transport
from .ssh_session import SSHSessionError
from .signing import device_identity


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--show-archive-public-key" in argv:
        enrollment = argparse.ArgumentParser(description="Display the public application-device identity for operator IAM enrollment; no network or private-key output")
        enrollment.add_argument("--show-archive-public-key", action="store_true", required=True)
        enrollment.add_argument("--journal", required=True, type=Path)
        selected = enrollment.parse_args(argv)
        journal = Journal(selected.journal)
        try:
            print(json.dumps(device_identity(journal), sort_keys=True))
        finally:
            journal.close()
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--show-archive-public-key", action="store_true", help="Use with --journal alone for offline operator enrollment material")
    parser.add_argument("--site", choices=("vega", "karolina"))
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--oauth-domain", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--callback-port", type=int, default=8765)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ssh-host", help="Existing own-account SSH configuration alias; BatchMode required")
    mode.add_argument("--local-cluster", action="store_true", help="Explicit institution-approved local execution")
    parser.add_argument("--expected-user", required=True, help="Your verified HPC username")
    parser.add_argument("--expected-host", required=True, help="Exact cluster hostname from hostname -f")
    parser.add_argument("--run-root", required=True, help="Existing private canonical SCRATCH compute parent on selected cluster")
    parser.add_argument("--archive-root", help="Separate existing private canonical PROJECT archive parent; cannot overlap run root")
    parser.add_argument("--durable-archive-root", action="store_true", help="Operator attests archive root is durable managed PROJECT storage with required retention; Karolina receipts require verified transfer here")
    parser.add_argument("--repository", required=True, help="Trusted scientific checkout on selected cluster")
    parser.add_argument("--model-registry", required=True, type=Path, help="Trusted operator-maintained JSON registry")
    parser.add_argument("--runtime-profile", type=Path, help="Trusted operator JSON: python_bin, environment_script, environment_sha256, site_runtime_root")
    parser.add_argument("--scheduler-config", type=Path, help="Trusted JSON Slurm policy: partition, account, qos, gres, cpus-per-task, time, mem")
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--poll-seconds", type=float, default=20)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if "HPC" + "_SITE" in os.environ:
        parser.error("Legacy site environment selector is unsupported; use MESOUQ_SITE")
    env_site = os.environ.get("MESOUQ_SITE")
    site = args.site or env_site
    if site not in {"vega", "karolina"} or (args.site and env_site and args.site != env_site):
        parser.error("Select matching --site/MESOUQ_SITE: vega or karolina")
    if args.poll_seconds < 1:
        parser.error("Poll interval must be at least one second")
    if args.durable_archive_root and not args.archive_root:
        parser.error("--durable-archive-root requires --archive-root separate from SCRATCH")
    transport = Transport(host=args.ssh_host, local=args.local_cluster)
    journal = None
    try:
        if transport.identity() != args.expected_user or transport.run(["hostname", "-f"]).strip() != args.expected_host:
            parser.error("HPC identity/hostname differs from the explicitly approved account and endpoint")
        token = browser_login(args.oauth_domain, args.client_id, args.callback_port)
        api = API(args.api_url, token)
        journal = Journal(args.journal)
        connector = Connector(api, Slurm(transport), journal, project=args.project, site=site,
                              run_root=args.run_root, repository=args.repository,
                              archive_root=args.archive_root, durable_archive_root=args.durable_archive_root,
                              model_registry=json.loads(args.model_registry.read_text()),
                              scheduler=json.loads(args.scheduler_config.read_text()) if args.scheduler_config else None,
                              runtime=json.loads(args.runtime_profile.read_text()) if args.runtime_profile else None)
        while True:
            connector.sweep()
            if args.once:
                break
            time.sleep(args.poll_seconds)
        return 0
    except SSHSessionError as error:
        print(json.dumps({"observed_at": time.time(), "connector_blocked": True, "error_category": error.category, "recovery_required": True}), flush=True)
        return 2
    finally:
        try:
            if journal is not None:
                journal.close()
        finally:
            transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
