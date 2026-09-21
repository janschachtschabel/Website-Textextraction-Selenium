"""Guards that the repository's own files agree with each other.

The supported Python versions must be the tested ones (A23), the image must install the
lockfile it ships, and .env.example must name every setting the service reads.
"""

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


def test_the_example_env_names_every_setting_the_service_reads():
    """.env.example is the operator's reference, and the only one once a key hides /docs."""
    readers = re.compile(r"(?:os\.getenv|os\.environ\.get|_bool)\(\s*[\"']([A-Z0-9_]+)[\"']")
    sources = [*sorted((ROOT / "app").rglob("*.py")), ROOT / "run.py"]
    read = {name for source in sources for name in readers.findall(source.read_text("utf-8"))}
    read -= {"XDG_CACHE_HOME"}  # freedesktop's own variable; RESULT_CACHE_DIR is the setting for it
    named = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", (ROOT / ".env.example").read_text("utf-8"), re.M))
    assert read <= named, f".env.example never names: {sorted(read - named)}"


def without_comments(name):
    """Compose files carry long comments; the assertions below are about the settings."""
    lines = (ROOT / name).read_text("utf-8").splitlines()
    return "\n".join(line for line in lines if not line.lstrip().startswith("#"))


def test_the_compose_file_a_panel_fetches_needs_no_checkout_and_no_variables():
    """A panel fetches this one file and pulls. A build context it does not have, or a
    required substitution it cannot fill, aborts the deployment before anything runs."""
    settings = without_comments("docker-compose.yml")
    assert "build:" not in settings, "a fetched compose file has no context to build from"
    assert ":?" not in settings, "a required variable aborts compose, and a panel fills none"
    assert re.search(r"image: \S+/\S+:\S+", settings), "the image must be pullable, not a local tag"


def test_the_compose_image_is_the_one_the_workflow_publishes_at_this_version():
    """A compose file naming an image nothing publishes is the failure this pair prevents."""
    name, tag = re.search(r"image: (\S+):(\S+)", without_comments("docker-compose.yml")).groups()
    published = re.search(r"IMAGE: (\S+)", without_comments(".github/workflows/publish.yml")).group(1)
    version = re.search(r'__version__ = "(.+)"', (ROOT / "app/__init__.py").read_text("utf-8")).group(1)
    assert name == published, f"compose pulls {name}, the workflow pushes {published}"
    assert tag == version, f"compose pulls {tag}, the code says {version}"


def test_a_checkout_still_builds_its_working_tree():
    """Without the override, `docker compose up --build` would silently run the published
    image instead of the change being tested."""
    override = without_comments("docker-compose.override.yml")
    assert "build:" in override and "API_KEY" in override
