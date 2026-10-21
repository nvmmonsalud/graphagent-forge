"""Scan tracked files; accept only individually reviewed false-positive fingerprints."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from detect_secrets import SecretsCollection
from detect_secrets.settings import default_settings


def fingerprint(filename: str, finding: dict) -> tuple[str, str, str]:
    return filename, finding["type"], finding["hashed_secret"]


def unreviewed(results: dict, reviewed: list[dict]) -> list[tuple[str, int, str]]:
    allowed = {fingerprint(item["filename"], item) for item in reviewed if item.get("reason")}
    # Review hashes themselves trigger entropy/keyword detectors. Accept only
    # these exact metadata values, never the rest of the review file's contents.
    for item in reviewed:
        digest = item["hashed_secret"]
        if len(digest) == 40 and all(char in "0123456789abcdef" for char in digest):
            metadata_hash = hashlib.sha1(digest.encode(), usedforsecurity=False).hexdigest()
            for detector in ("Secret Keyword", "Hex High Entropy String"):
                allowed.add((".security/reviewed-secrets.json", detector, metadata_hash))
    return [
        (filename, finding["line_number"], finding["type"])
        for filename, findings in results.items()
        for finding in findings
        if fingerprint(filename, finding) not in allowed
    ]


def main() -> int:
    files = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    secrets = SecretsCollection()
    # Use scanner defaults, including heuristic false-positive filters. Never verify
    # candidate credentials with external services or emit their values in logs.
    with default_settings():
        for filename in files:
            if filename and Path(filename).is_file():
                secrets.scan_file(filename)
    reviewed = json.loads(Path(".security/reviewed-secrets.json").read_text())
    findings = unreviewed(secrets.json(), reviewed)
    for filename, line, kind in findings:
        print(f"{filename}:{line}: unreviewed {kind}")
    print(f"Secret scan: {len(files) - 1} tracked files, {len(findings)} unreviewed findings")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
