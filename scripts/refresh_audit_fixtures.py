"""Rebuild the offline "checked against reality" fixtures.

For each fixture project under tests/fixtures/npm/<name>/ (package-lock.json or
package-lock.v3.json, plus npm-audit.json), take every OSV record from a full
export that mentions any package in the lockfile, and write them to
tests/fixtures/osv/audit-subset.zip. Records that *don't* affect the installed
versions are included on purpose: the test then also proves we don't over-match.

Usage (after downloading https://osv-vulnerabilities.storage.googleapis.com/npm/all.zip):

    python scripts/refresh_audit_fixtures.py path/to/all.zip

Regenerate the npm-audit.json snapshots at the same time, so advisories and
audit results describe the same moment (see tests/fixtures/README.md).
"""

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ["vulnerable", "realworld"]


def lockfile_packages(project: Path) -> set[str]:
    lock = next(p for p in (project / "package-lock.json", project / "package-lock.v3.json")
                if p.exists())  # fmt: skip
    names = set()
    for path, entry in json.loads(lock.read_text(encoding="utf-8"))["packages"].items():
        if path:
            names.add(entry.get("name") or path.rpartition("node_modules/")[2])
    return names


def main(export: Path) -> None:
    wanted: set[str] = set()
    for name in PROJECTS:
        wanted |= lockfile_packages(ROOT / "tests" / "fixtures" / "npm" / name)

    out = ROOT / "tests" / "fixtures" / "osv" / "audit-subset.zip"
    kept = 0
    with zipfile.ZipFile(export) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info)
            doc = json.loads(data)
            packages = {
                a.get("package", {}).get("name")
                for a in doc.get("affected", [])
                if a.get("package", {}).get("ecosystem") == "npm"
            }
            if packages & wanted:
                dst.writestr(info.filename, data)
                kept += 1
    print(f"{kept} records for {len(wanted)} packages -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
