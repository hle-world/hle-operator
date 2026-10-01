"""Tests for the Helm chart's release-tracking contract.

The chart is published as an OCI artifact with its version and appVersion set
from the release tag at package time (see .github/workflows/build.yml). These
tests pin the parts that make an unpinned `helm install oci://.../hle-operator`
resolve to the release's image.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CHART_DIR = Path(__file__).resolve().parents[1] / "chart" / "hle-operator"


def _load(name: str) -> dict:
    with (CHART_DIR / name).open() as handle:
        return yaml.safe_load(handle)


class TestChartMetadata:
    def test_chart_identity(self):
        chart = _load("Chart.yaml")
        assert chart["apiVersion"] == "v2"
        assert chart["name"] == "hle-operator"
        assert chart["type"] == "application"

    def test_static_version_matches_app_version(self):
        chart = _load("Chart.yaml")
        # YAML parses the unquoted `version` as a float, so normalise it; the
        # two fields are bumped together and must stay in lockstep.
        assert str(chart["version"]) == chart["appVersion"]


class TestImageTagDefaults:
    def test_operator_image_tag_falls_back_to_app_version(self):
        values = _load("values.yaml")
        assert values["image"]["tag"] == ""
        assert values["image"]["pullPolicy"] == "IfNotPresent"

        template = (CHART_DIR / "templates" / "deployment.yaml").read_text()
        assert ".Values.image.tag | default .Chart.AppVersion" in template
