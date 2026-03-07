# HLE Operator

Kubernetes operator for [HLE (Home Lab Everywhere)](https://hle.world) tunnels. Expose your cluster services to the internet through HLE's relay network with declarative Kubernetes resources.

## Features

- **HLETunnel CRD** — Declarative tunnel management with full access control
- **Ingress support** — Use `ingressClassName: hle` on standard Ingress resources
- **Access control** — SSO email allowlists (per-provider), PIN protection, Basic Auth
- **Sync policies** — `strict` (operator is source of truth) or `initial` (dashboard stays editable)
- **Smart updates** — Access control changes don't restart tunnels
- **Per-tunnel API keys** — Override the global key for multi-tenant setups

## Quick Start

### 1. Install the operator

```bash
# Create the API key Secret
kubectl create secret generic hle-api-key \
  --from-literal=api-key=hle_your_key_here

# Install via Helm
helm install hle-operator ./chart/hle-operator
```

### 2. Expose a service

**Using HLETunnel CRD:**

```yaml
apiVersion: hle.world/v1alpha1
kind: HLETunnel
metadata:
  name: grafana
spec:
  serviceRef:
    name: grafana
    namespace: monitoring
    port: 3000
  label: grafana
  authMode: sso
  syncPolicy: strict
  accessControl:
    allowedUsers:
      - email: me@gmail.com
        provider: google
      - email: dev@company.com
        provider: github
    pin: "1234"
```

**Using Ingress:**

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: grafana
  annotations:
    hle.world/label: grafana
    hle.world/auth-mode: sso
    hle.world/allowed-users: "me@gmail.com:google,dev@company.com:github"
    hle.world/pin: "1234"
spec:
  ingressClassName: hle
  rules:
  - http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: grafana
            port:
              number: 3000
```

### 3. Check status

```bash
kubectl get hlt
# NAME      PHASE       PUBLIC URL                        LABEL     SYNC     AGE
# grafana   Connected   https://grafana-x7k.hle.world     grafana   strict   5m
```

## CRD Reference

### spec.serviceRef

| Field | Type | Required | Description |
|---|---|---|---|
| `name` | string | yes | Kubernetes Service name |
| `namespace` | string | no | Service namespace (defaults to HLETunnel namespace) |
| `port` | integer | yes | Service port |
| `protocol` | string | no | `http` (default) or `https` |

### spec.accessControl

| Field | Type | Description |
|---|---|---|
| `allowedUsers` | array | List of `{email, provider}` objects. Provider: `google`, `github`, `hle`, `any` |
| `pin` | string | 4-8 digit PIN code |
| `basicAuth.secretRef.name` | string | Secret with `username` and `password` keys |

### spec.syncPolicy

- **`strict`** (default) — Operator continuously reconciles access control. Dashboard edits are overwritten every 60 seconds.
- **`initial`** — Operator applies settings only on tunnel creation. Dashboard remains fully editable after that.

### spec.apiKeyRef

Override the global API key for this specific tunnel:

```yaml
spec:
  apiKeyRef:
    name: my-other-api-key  # Secret name
    key: api-key             # Key within the Secret
```

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                  Kubernetes Cluster                  │
│                                                      │
│  ┌──────────────┐    ┌─────────────────────────┐    │
│  │ HLE Operator │───→│ Service (grafana:3000)   │    │
│  │   (kopf)     │    │ Service (argocd:8080)    │    │
│  └──────┬───────┘    │ Service (longhorn:8000)  │    │
│         │            └─────────────────────────┘    │
└─────────┼───────────────────────────────────────────┘
          │ WebSocket tunnel (per service)
          ▼
┌─────────────────────┐
│   hle.world relay   │ ← Assigns subdomains, handles SSO
│   (VPS Amsterdam)   │
└─────────────────────┘
          ▲
          │ HTTPS
          │
    End users access:
    grafana-x7k.hle.world
    argocd-x7k.hle.world
```

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run kopf run src/hle_operator/handlers.py --verbose
uv run ruff check src/
uv run pytest tests/ -v
```

## License

MIT
