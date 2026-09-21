"""The supported Python versions must be the tested ones (audit finding A23)."""

import re
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
