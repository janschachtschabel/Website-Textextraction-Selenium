"""Guards that the repository's own files agree with each other.

The supported Python versions must be the tested ones (A23), the image must install the
lockfile it ships, and every setting the service reads must appear in both files the
operator consults about settings: .env.example lists them, docs/settings.md explains them.
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
    """Requirement lines of a pip-compile lockfile, with the hashes that follow each: a pinned
    version, or a direct reference such as a spaCy model wheel (name @ url)."""
    joined = text.replace(chr(92) + chr(10), " ")  # a backslash at end of line continues it
    found = {}
    for line in joined.splitlines():
        line = line.split("#")[0].strip()
        match = re.match(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?(?:==| @ )", line)
        if match:
            found[match.group(1).lower().replace("_", "-")] = re.findall(r"--hash=sha256:[0-9a-f]{64}", line)
    return found


def declared():
    """Direct dependencies of the project and of the extras the image installs.

    Read with tomllib rather than a regex: the lists hold extras of their own, so
    "uvicorn[standard]" ends a naive bracket match after the second entry.
    """
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    extras = metadata["optional-dependencies"]
    specs = metadata["dependencies"] + extras["documents"] + extras["pii"]
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


def test_the_default_anonymization_models_are_the_ones_the_image_installs():
    """anonymize loads the model a setting names and never downloads one: a default the image
    lacks answers every anonymized request with 503, as 3.0.0 did."""
    config = (ROOT / "app/config.py").read_text("utf-8")
    defaults = re.findall(r'os\.getenv\("PRESIDIO_(?:DE|EN)_MODEL", "([A-Za-z0-9_]+)"\)', config)
    locked = requirements((ROOT / "requirements.lock").read_text("utf-8"))
    assert len(defaults) == 2, f"expected a German and an English default, found {defaults}"
    missing = sorted(model for model in defaults if model.replace("_", "-") not in locked)
    assert not missing, f"the image does not install the default models {missing}"


def test_the_image_ships_ffprobe_for_media_metadata():
    """media_conversion_policy=metadata runs ffprobe; without it the answer is failed."""
    installs = re.findall(r"apt-get install [^\n]*", (ROOT / "Dockerfile").read_text("utf-8"))
    assert any(re.search(r"\bffmpeg\b", line) for line in installs), "Debian ships ffprobe in its ffmpeg package"


def settings_the_service_reads():
    """Every environment variable app/ and run.py actually read. Two files answer for these
    to the operator - one lists them, one explains them - and both are guarded below."""
    readers = re.compile(r"(?:os\.getenv|os\.environ\.get|_bool)\(\s*[\"']([A-Z0-9_]+)[\"']")
    sources = [*sorted((ROOT / "app").rglob("*.py")), ROOT / "run.py"]
    read = {name for source in sources for name in readers.findall(source.read_text("utf-8"))}
    return read - {"XDG_CACHE_HOME"}  # freedesktop's own variable; RESULT_CACHE_DIR is the setting for it


def test_the_example_env_names_every_setting_the_service_reads():
    """.env.example is the operator's reference for what a deployment can be told."""
    named = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", (ROOT / ".env.example").read_text("utf-8"), re.M))
    read = settings_the_service_reads()
    assert read <= named, f".env.example never names: {sorted(read - named)}"


def test_the_settings_reference_explains_every_setting_the_service_reads():
    """.env.example is a plain NAME=VALUE list on purpose, so that a panel's environment
    editor can take it whole; the explanations live in docs/settings.md instead. That file
    had nothing watching it. 2.3.1 was exactly this drift one file over - a limit the code
    had made configurable while the text about it still named the old constant.

    A table row, not a mention: the row is what carries the default and the description an
    operator looks the setting up for."""
    documented = (ROOT / "docs/settings.md").read_text("utf-8")
    missing = sorted(
        name for name in settings_the_service_reads() if not re.search(rf"^\| `{name}` \|", documented, re.M)
    )
    assert not missing, f"docs/settings.md has no table row for: {missing}"


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


def test_the_settings_example_is_plain_rows_anything_can_paste():
    """A panel's variable editor reads one NAME=VALUE per row and has no idea what a # line
    is. Six comments here became six invalid entries in one, and the explanations moved to
    docs/settings.md so this file can stay pasteable."""
    lines = (ROOT / ".env.example").read_text("utf-8").splitlines()
    unpastable = [line for line in lines if not re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", line)]
    assert not unpastable, f".env.example holds rows that are not NAME=VALUE: {unpastable}"
