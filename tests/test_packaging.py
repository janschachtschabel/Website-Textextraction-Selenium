"""The supported Python versions must be the tested ones (audit finding A23)."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_ci_tests_every_python_version_the_metadata_allows():
    metadata = re.search(r'requires-python = ">=3\.(\d+),<3\.(\d+)"', (ROOT / "pyproject.toml").read_text("utf-8"))
    allowed = {f"3.{minor}" for minor in range(int(metadata.group(1)), int(metadata.group(2)))}
    workflow = (ROOT / ".github/workflows/ci.yml").read_text("utf-8")
    tested = set(re.search(r"python: \[(.+)\]", workflow).group(1).replace("'", "").split(", "))
    assert tested == allowed


def test_the_image_pins_a_python_the_metadata_allows():
    """The Dockerfile pins one version; requires-python moving would break it silently."""
    metadata = re.search(r'requires-python = ">=3\.(\d+),<3\.(\d+)"', (ROOT / "pyproject.toml").read_text("utf-8"))
    allowed = {f"3.{minor}" for minor in range(int(metadata.group(1)), int(metadata.group(2)))}
    pinned = set(re.findall(r"FROM python:(3\.\d+)-", (ROOT / "Dockerfile").read_text("utf-8")))
    assert pinned and pinned <= allowed, f"Dockerfile builds on {pinned}, metadata allows {sorted(allowed)}"


def requirements(text):
    """Requirement lines of a pip-compile lockfile, with the hashes that follow each."""
    joined = text.replace(chr(92) + chr(10), " ")  # a backslash at end of line continues it
    found = {}
    for line in joined.splitlines():
        line = line.split("#")[0].strip()
        match = re.match(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?==", line)
        if match:
            found[match.group(1).lower().replace("_", "-")] = re.findall(r"--hash=sha256:[0-9a-f]{64}", line)
    return found


def declared():
    """Direct dependencies of the project and of the extra the image installs.

    Read with tomllib rather than a regex: the lists hold extras of their own, so
    "uvicorn[standard]" ends a naive bracket match after the second entry.
    """
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    specs = metadata["dependencies"] + metadata["optional-dependencies"]["documents"]
    return {re.match(r"[A-Za-z0-9._-]+", spec).group(0).lower().replace("_", "-") for spec in specs}


def test_the_lockfile_pins_every_dependency_with_a_hash():
    """A version pin without a hash accepts a re-uploaded artefact; the image requires both."""
    locked = requirements((ROOT / "requirements.lock").read_text("utf-8"))
    assert locked, "the lockfile carries no requirements"
    unhashed = sorted(name for name, hashes in locked.items() if not hashes)
    assert not unhashed, f"pinned without a hash: {unhashed}"
    missing = sorted(declared() - set(locked))
    assert not missing, f"declared but not locked: {missing}"


def test_the_image_installs_from_the_lockfile_and_checks_the_hashes():
    dockerfile = (ROOT / "Dockerfile").read_text("utf-8")
    assert "requirements.lock" in dockerfile, "the image resolves its own versions instead"
    assert "--require-hashes" in dockerfile, "a lockfile whose hashes are not checked proves nothing"
