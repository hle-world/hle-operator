# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Kubernetes operator for [HLE (Home Lab Everywhere)](https://hle.world) tunnels. Watches `HLETunnel` CRDs and Ingress resources with `ingressClassName: hle` to automatically create, manage, and secure HLE tunnels to Kubernetes services.

Built with Python using [kopf](https://kopf.readthedocs.io/) (Kubernetes Operator Pythonic Framework) and the [hle-client](https://pypi.org/project/hle-client/) library.

## Development Commands

```bash
# Setup
uv venv && uv pip install -e ".[dev]"

# Run locally (uses kubeconfig)
uv run kopf run src/hle_operator/handlers.py --verbose

# Lint & type check
uv run ruff check src/
uv run ruff format --check src/
uv run mypy --strict src/

# Tests
uv run pytest tests/ -v
uv run pytest tests/ -k "test_parse_crd" -v

# Build container
docker build -t hle-operator:dev .

# Helm install (requires hle-api-key Secret to exist)
helm install hle-operator ./chart/hle-operator --set image.tag=dev
```

## Releasing

Releases are CalVer (`v2610.4`). Bump the in-repo chart and merge it *before*
tagging, or a clone installs a stale image: `./scripts/release.sh v2610.5` rewrites
`chart/hle-operator/Chart.yaml` and prints the diff, then commit on
`chore/release-v2610.5`, open a PR, merge, and run `gh release create v2610.5`.
`build.yml` still overrides the version inside the published artifact.

## Architecture

### Two input paths, one tunnel manager

```
HLETunnel CRD ──→ parse_crd_spec() ──→ ManagedTunnelSpec ──→ ManagedTunnel ──→ hle-client Tunnel
Ingress (hle) ──→ parse_ingress_annotations() ──────────────────┘
```

Both CRDs and annotated Ingress resources are normalized into `ManagedTunnelSpec`, then managed identically.

### Key modules

- **`handlers.py`** — Kopf event handlers. Thin layer that parses K8s resources, resolves Secrets, and delegates to `ManagedTunnel`. Maintains two registries: `_tunnels` (CRD-managed) and `_ingress_tunnels` (Ingress-managed), keyed by `"namespace/name"`.

- **`tunnel_manager.py`** — Core logic. `ManagedTunnel` wraps hle-client's `Tunnel` class, adding access control reconciliation (email rules, PIN, basic auth) via the relay REST API. Contains the two parser functions (`parse_crd_spec`, `parse_ingress_annotations`) that normalize K8s resources into `ManagedTunnelSpec`.

- **`config.py`** — Loads K8s config (in-cluster or kubeconfig) and the global HLE API key from env or Secret.

### Tunnel lifecycle

1. Kopf handler fires (create/resume/update)
2. Spec parsed → `ManagedTunnelSpec`
3. Secrets resolved (basic auth, per-tunnel API key)
4. `ManagedTunnel.start()` creates an `hle_client.tunnel.Tunnel` and runs `connect()` as a background `asyncio.Task`
5. `on_registered` callback fires when relay assigns a subdomain
6. Access control reconciled via relay REST API (`ApiClient`)
7. Status patched on the CRD (`phase`, `publicUrl`, `subdomain`)

### syncPolicy behavior

- **`strict`** (default) — `@kopf.timer` reconciles access control every 60s. Dashboard edits are overwritten. Future: server-side `managed_by` field will lock dashboard UI.
- **`initial`** — Access control applied only on tunnel creation. Dashboard remains fully editable.

### Smart updates

When only `spec.accessControl` changes, the operator reconciles access rules without restarting the tunnel connection. Tunnel config changes (serviceRef, label, authMode, etc.) trigger a full stop/start cycle.

## CRD Schema

Group: `hle.world`, Version: `v1alpha1`, Kind: `HLETunnel`, Short name: `hlt`

Required fields: `spec.serviceRef` (name + port) and `spec.label`.

Printer columns: `kubectl get hlt` shows Phase, Public URL, Label, Sync Policy, Age.

## Ingress Annotations

All annotations use the `hle.world/` prefix:

| Annotation | Description | Default |
|---|---|---|
| `hle.world/label` | Tunnel label (subdomain prefix) | Service name |
| `hle.world/auth-mode` | `sso` or `none` | `sso` |
| `hle.world/sync-policy` | `strict` or `initial` | `strict` |
| `hle.world/allowed-users` | `email:provider,...` format | (none) |
| `hle.world/pin` | 4-8 digit PIN | (none) |
| `hle.world/basic-auth-secret` | Secret name with username/password | (none) |
| `hle.world/protocol` | `http` or `https` for upstream | `http` |
| `hle.world/websocket-enabled` | Enable WS proxying | `true` |
| `hle.world/forward-host` | Forward browser Host header | `false` |
| `hle.world/verify-ssl` | Verify upstream SSL | `false` |

## Helm Chart

Located in `chart/hle-operator/`. Deploys:
- CRD (`HLETunnel`)
- Deployment (single replica, kopf has built-in leader election via peering)
- ServiceAccount + ClusterRole + ClusterRoleBinding
- Liveness probe on `:8080/healthz`

The API key is injected from a Secret via `values.apiKey.secretName` / `values.apiKey.secretKey`.

## Dependencies on Other HLE Repos

- **hle-client** (`hle_client.tunnel.Tunnel`, `hle_client.api.ApiClient`) — used as a library, not CLI. The operator imports it directly for tunnel connections and relay API calls.
- **hle server** — the relay that assigns subdomains and hosts the access control REST API. Future work: add `managed_by` field to `TunnelRegistration` so the dashboard can show "managed by operator" and lock edits for strict-mode tunnels.
