#!/usr/bin/env python3
"""Check tracked/candidate source files without displaying sensitive matches."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess


FORBIDDEN_PARTS = {".aws", ".ssh", ".local", ".publishing-local", ".terraform", "node_modules", "__pycache__", ".venv", "venv", "build", "dist"}
FORBIDDEN_SUFFIXES = {".tfstate", ".tfplan", ".pem", ".key", ".p12", ".pfx", ".ppk", ".pkl", ".pt", ".safetensors", ".ckpt", ".onnx", ".h5", ".hdf5", ".npz", ".npy", ".whl", ".pyc"}
FORBIDDEN_NAMES = {"journal.json", "session.lock", "credentials", "authorized_keys", "id_rsa", "id_ed25519", "archive-device.json"}
KEY_BLOCK = re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{100,}-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
ACCESS_KEY = re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")


def check(root: Path, private_strings=()) -> list[dict[str, str]]:
    names = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
                           cwd=root, capture_output=True, check=True).stdout.decode().split("\0")
    problems = []
    for name in sorted(set(filter(None, names))):
        path = root / name
        leaf = path.name
        private_environment = (leaf == ".env" or leaf.startswith(".env.")) and leaf != ".env.example"
        private_variables = (leaf.endswith(".tfvars") or leaf.endswith(".tfvars.json")) and not leaf.endswith(".example.tfvars")
        if (set(path.relative_to(root).parts) & FORBIDDEN_PARTS or path.suffix in FORBIDDEN_SUFFIXES
                or leaf in FORBIDDEN_NAMES or private_environment or private_variables or ".tfstate." in name):
            problems.append({"file": name, "reason": "private or generated artifact"})
            continue
        if path.is_symlink() or not path.is_file():
            problems.append({"file": name, "reason": "unsupported source entry"})
            continue
        blob = path.read_bytes()
        if KEY_BLOCK.search(blob) or ACCESS_KEY.search(blob):
            problems.append({"file": name, "reason": "credential pattern"})
        if any(value.encode() in blob for value in private_strings):
            problems.append({"file": name, "reason": "operator-specific identifier"})
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-pattern-file", type=Path,
                        help="Optional ignored local JSON array of additional private strings")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    patterns = json.loads(args.private_pattern_file.read_text()) if args.private_pattern_file else []
    if not isinstance(patterns, list) or any(not isinstance(v, str) or len(v) < 3 for v in patterns):
        parser.error("Private patterns must be nonempty strings of at least three characters")
    problems = check(root, patterns)
    print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
    return bool(problems)


if __name__ == "__main__":
    raise SystemExit(main())
