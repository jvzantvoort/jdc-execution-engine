#!/usr/bin/env python3
"""Check that the EE's Python dependencies are installed and locked.

Usage: check_python_deps.py CONSTRAINTS [REQUIREMENTS ...]

Runs inside the image. Collects the Python requirements of
  - each REQUIREMENTS file (the repo's requirements.txt), and
  - every installed collection, found the way ansible-builder finds them:
    meta/execution-environment.yml `dependencies.python` (a file or a list),
    else requirements.txt at the collection root.

For each requirement that applies to this Python (environment markers), it
checks that the package is installed, that it satisfies the requirement, and
that CONSTRAINTS pins it to exactly the installed version. Then `pip check`
must be clean, so transitive dependencies are consistent too.

A package missing from the lock file is installed at whatever version is
newest at build time, which breaks reproducible builds: fix with `make lock`.
"""
import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import yaml
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

# ansible-builder never installs these from collection requirements
# (ansible_builder/_target_scripts/introspect.py: EXCLUDE_REQUIREMENTS)
BUILDER_EXCLUDES = {
    canonicalize_name(n)
    for n in ("ansible", "ansible-base", "ansible-core", "ansible-runner",
              "pytest", "python", "setuptools", "six", "tox")
}


def parse_lines(lines, source):
    """Requirement objects from requirements.txt lines; options and comments skipped."""
    reqs = []
    for raw in lines:
        line = raw.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-")):
            continue
        try:
            reqs.append((Requirement(line), source))
        except InvalidRequirement as exc:
            print(f"WARN: {source}: cannot parse {line!r}: {exc}")
    return reqs


def collection_requirements(path, fqcn):
    meta = path / "meta" / "execution-environment.yml"
    python = None
    if meta.is_file():
        data = yaml.safe_load(meta.read_text()) or {}
        python = (data.get("dependencies") or {}).get("python")
    if python is None:
        python = "requirements.txt"
    if isinstance(python, list):
        return parse_lines(python, fqcn)
    req_file = path / python
    if not req_file.is_file():
        return []
    return parse_lines(req_file.read_text().splitlines(), fqcn)


def installed_collections():
    out = subprocess.run(
        ["ansible-galaxy", "collection", "list", "--format", "json"],
        check=True, capture_output=True, text=True,
    ).stdout
    seen = {}
    for root, colls in json.loads(out).items():
        for fqcn in colls:
            ns, name = fqcn.split(".", 1)
            seen.setdefault(fqcn, Path(root) / ns / name)  # first path wins, like Ansible
    return seen


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    pins = {}
    for req, _ in parse_lines(Path(argv[1]).read_text().splitlines(), argv[1]):
        spec = list(req.specifier)
        if len(spec) == 1 and spec[0].operator == "==":
            pins[canonicalize_name(req.name)] = spec[0].version

    reqs = []
    for f in argv[2:]:
        reqs += parse_lines(Path(f).read_text().splitlines(), Path(f).name)
    collections = installed_collections()
    for fqcn, path in sorted(collections.items()):
        found = collection_requirements(path, fqcn)
        reqs += [(r, s) for r, s in found if canonicalize_name(r.name) not in BUILDER_EXCLUDES]
        if found:
            print(f"{fqcn}: {', '.join(sorted(str(r) for r, _ in found))}")

    errors = []
    checked = set()
    for req, source in reqs:
        if req.marker and not req.marker.evaluate():
            continue
        name = canonicalize_name(req.name)
        try:
            version = metadata.version(req.name)
        except metadata.PackageNotFoundError:
            errors.append(f"{req.name} (from {source}) is not installed")
            continue
        if not req.specifier.contains(version, prereleases=True):
            errors.append(f"{req.name} {version} does not satisfy {req} (from {source})")
        if name not in pins:
            errors.append(f"{req.name} (from {source}) is not pinned in {argv[1]}; run `make lock`")
        elif pins[name] != version:
            errors.append(f"{req.name} is {version} but {argv[1]} pins {pins[name]}")
        checked.add(name)

    pip = subprocess.run([sys.executable, "-m", "pip", "check"], capture_output=True, text=True)
    if pip.returncode != 0:
        errors.append("pip check: " + (pip.stdout + pip.stderr).strip())

    for e in errors:
        print(f"FAIL: {e}", file=sys.stderr)
    if errors:
        return 1
    print(f"OK: {len(checked)} Python requirements from requirements.txt and "
          f"{len(collections)} collections are installed and locked; pip check clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
