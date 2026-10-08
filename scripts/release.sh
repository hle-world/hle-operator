#!/usr/bin/env bash
#
# Bump chart/hle-operator/Chart.yaml to a release tag before tagging.
#
# The release workflow only overrides the chart version and appVersion inside
# the packaged artifact (see .github/workflows/build.yml, job "chart"); nothing
# writes them back to git, so the in-repo copy drifts behind the newest release.
# Run this on a branch, open a PR, merge it, then create the release. It never
# tags, pushes or calls gh.
#
# Usage: ./scripts/release.sh v2610.5

set -euo pipefail

tag="${1:-}"

# Release tags are CalVer with a leading "v" (v2610.4). The workflow's version
# rules are mirrored here: appVersion is the tag without the "v", and the Helm
# version appends ".0" when the CalVer has only two parts.
if ! printf '%s' "$tag" | grep -Eq '^v[0-9]{4}\.[0-9]+(\.[0-9]+)?$'; then
  echo "usage: $0 v<YYMM>.<N>[.<P>]  (got: '${tag:-}')" >&2
  exit 1
fi

# Resolve paths relative to the repo root, not the caller's cwd.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
chart="${repo_root}/chart/hle-operator/Chart.yaml"

app="${tag#v}"
chart_version="$app"
if printf '%s' "$app" | grep -Eq '^[0-9]+\.[0-9]+$'; then
  chart_version="${app}.0"
fi

# sed -i.bak works on both GNU and BSD/macOS sed; the backup is dropped after.
sed -i.bak -E \
  -e "s/^version: .*/version: \"${chart_version}\"/" \
  -e "s/^appVersion: .*/appVersion: \"${app}\"/" \
  "$chart"
rm -f "${chart}.bak"

echo "Bumped chart/hle-operator/Chart.yaml to version ${chart_version} / appVersion ${app}"
echo
git -C "$repo_root" diff -- chart/hle-operator/Chart.yaml
echo
echo "Next steps:"
echo "  git checkout -b chore/release-${tag}"
echo "  git commit -am \"chore(chart): bump to ${app}\""
echo "  gh pr create"
echo "  # once the PR is merged:"
echo "  gh release create ${tag}"
