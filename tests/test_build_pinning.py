"""The image build is reproducible: locked dependencies, pinned base image and actions."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_base_image_is_pinned_by_digest():
    from_lines = [
        line for line in (ROOT / "Dockerfile").read_text().splitlines() if line.startswith("FROM ")
    ]
    assert from_lines
    for line in from_lines:
        assert re.fullmatch(r"FROM \S+@sha256:[0-9a-f]{64}( AS \w+)?", line), line


def test_dockerfile_installs_only_from_the_hashed_lock():
    text = (ROOT / "Dockerfile").read_text()
    assert "--require-hashes" in text
    assert "requirements.lock" in text
    # The only pip install is the hashed one: nothing resolves at build time.
    installs = [line for line in text.splitlines() if "pip install" in line]
    assert len(installs) == 1


def test_every_action_is_pinned_to_a_commit():
    unpinned = []
    for workflow in (ROOT / ".github" / "workflows").glob("*.yml"):
        for number, line in enumerate(workflow.read_text().splitlines(), 1):
            match = re.search(r"uses:\s*(\S+)", line)
            if match and not re.fullmatch(r"[^@]+@[0-9a-f]{40}", match.group(1)):
                unpinned.append(f"{workflow.name}:{number}: {match.group(1)}")
    assert unpinned == []


def test_lock_requires_hle_client_from_pyproject():
    lock = (ROOT / "requirements.lock").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()
    minimum = re.search(r'"hle-client>=([0-9.]+)"', pyproject)
    pinned = re.search(r"^hle-client==([0-9.]+)", lock, re.MULTILINE)
    assert minimum and pinned
    as_tuple = lambda v: tuple(int(p) for p in v.split("."))  # noqa: E731
    assert as_tuple(pinned.group(1)) >= as_tuple(minimum.group(1))


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_lock_matches_uv_lock():
    out = subprocess.run(  # noqa: S603
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--format",
            "requirements-txt",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout

    def body(text: str) -> str:
        return "\n".join(line for line in text.splitlines() if not line.startswith("#"))

    assert body(out) == body((ROOT / "requirements.lock").read_text()), (
        "requirements.lock is stale: uv export --frozen --no-dev --no-emit-project "
        "--format requirements-txt -o requirements.lock"
    )
