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
