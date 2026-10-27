#!/usr/bin/env python3
"""Run the pinned official OIDC client without publishing its diagnostic output."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

ACTION_COMMIT = "e1253824e5c10ff9df46874f81ed3ec929e19cfd"


def action_environment(phase, source=None):
    env = dict(os.environ if source is None else source)
    for name in list(env):
        if name.startswith("INPUT_"):
            env.pop(name)
    env["INPUT_OUTPUT-ENV-CREDENTIALS"] = "true"
    if phase == "cleanup":
        return env
    role = env.get("PUBLICATION_ROLE_ARN", "")
    region = env.get("PUBLICATION_REGION", "")
    run = env.get("GITHUB_RUN_ID", "")
    policy = env.get("PUBLICATION_SESSION_POLICY", "")
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/[A-Za-z0-9_+=,.@/-]+", role)
    if match is None or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-[0-9]+", region) or not run.isdigit():
        raise ValueError("authentication-input-invalid")
    if not isinstance(json.loads(policy), dict):
        raise ValueError("authentication-input-invalid")
    # Select OIDC explicitly; do not fall back to ambient owner/profile credentials.
    for name in list(env):
        if name.startswith("AWS_"):
            env.pop(name)
    values = {
        "aws-region": region, "role-to-assume": role,
        "role-session-name": "website-" + run, "role-duration-seconds": "900",
        "mask-aws-account-id": "true", "output-credentials": "false",
        "output-env-credentials": "true", "unset-current-credentials": "true",
        "translate-env-variables": "false", "audience": "sts.amazonaws.com",
        "inline-session-policy": policy, "allowed-account-ids": match[1],
        "use-existing-credentials": "", "force-skip-oidc": "false",
        "role-chaining": "false",
    }
    env.update({"INPUT_" + key.upper(): value for key, value in values.items()})
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    return env


def stream_action(command, env, output, popen=None):
    """Forward only the normal mask protocol; never retain authentication logs."""
    popen = subprocess.Popen if popen is None else popen
    process = popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace")
    for line in process.stdout:
        if (line.startswith("::add-mask::") and line.endswith("\n") and
                "\r" not in line and len(line) <= 65536):
            output.write(line)
            output.flush()
    return process.wait()


def run(directory, phase, output=None):
    output = sys.stdout if output is None else output
    try:
        root = Path(directory).resolve(strict=True)
        check = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=10)
        if check.returncode or check.stdout.strip() != ACTION_COMMIT:
            raise ValueError("authentication-client-invalid")
        clean = subprocess.run(["git", "-C", str(root), "diff", "--quiet", "HEAD", "--",
                                "dist/index.js", "dist/cleanup/index.js"],
                               capture_output=True, timeout=10)
        version = subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=10)
        bundle = root / ("dist/index.js" if phase == "configure" else "dist/cleanup/index.js")
        if (clean.returncode or version.returncode or not re.fullmatch(r"v24\.[0-9]+\.[0-9]+\s*", version.stdout)
                or not bundle.is_file() or bundle.is_symlink() or not shutil.which("timeout")):
            raise ValueError("authentication-client-invalid")
        env = action_environment(phase)
        status = stream_action(["timeout", "--kill-after=5s",
                                "180s" if phase == "configure" else "30s",
                                "node", str(bundle)], env, output)
    except Exception:
        print(json.dumps({"status": "failed", "category": "authentication-client-failed"}), file=output)
        return 1
    if status:
        print(json.dumps({"status": "failed", "category": "scoped-authentication-failed"}), file=output)
        return status if status > 0 else 1
    print(json.dumps({"status": "verified", "phase": phase}), file=output)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action-directory", required=True)
    parser.add_argument("--phase", choices=("configure", "cleanup"), required=True)
    args = parser.parse_args()
    return run(args.action_directory, args.phase)


if __name__ == "__main__":
    raise SystemExit(main())
