{{/*
Expand the name of the chart.
*/}}
{{- define "hle-operator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "hle-operator.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Common labels.
*/}}
{{- define "hle-operator.labels" -}}
helm.sh/chart: {{ include "hle-operator.name" . }}-{{ .Chart.Version | replace "+" "_" }}
{{ include "hle-operator.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels.
*/}}
{{- define "hle-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "hle-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
ServiceAccount name.
*/}}
{{- define "hle-operator.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "hle-operator.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Agent name. Suffixed rather than sharing the operator's name so the two can be
told apart in `kubectl get pods`, and so either can be installed alone.
*/}}
{{- define "hle-operator.agentFullname" -}}
{{- printf "%s-agent" (include "hle-operator.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Agent selector labels. The component label is what keeps the agent's Deployment
selector from also matching the operator's pods.
*/}}
{{- define "hle-operator.agentSelectorLabels" -}}
app.kubernetes.io/name: {{ include "hle-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: agent
{{- end }}

{{/*
Agent common labels.
*/}}
{{- define "hle-operator.agentLabels" -}}
helm.sh/chart: {{ include "hle-operator.name" . }}-{{ .Chart.Version | replace "+" "_" }}
{{ include "hle-operator.agentSelectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Agent ServiceAccount name.
*/}}
{{- define "hle-operator.agentServiceAccountName" -}}
{{- if .Values.agent.serviceAccount.create }}
{{- default (include "hle-operator.agentFullname" .) .Values.agent.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.agent.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Resolve the operator mode: "agent" (default) or "legacy".

`mode` wins when set. Otherwise auto-detect, mirroring the runtime in
hle_operator.config.resolve_mode: an install that holds only a full-account
`apiKey` and no agent credential stays legacy; everything else is agent.
*/}}
{{- define "hle-operator.mode" -}}
{{- $explicit := .Values.mode | default "" | lower -}}
{{- if eq $explicit "agent" -}}
agent
{{- else if eq $explicit "legacy" -}}
legacy
{{- else -}}
{{- $hasCredential := or .Values.credential.value .Values.credential.existingSecret -}}
{{- $hasAgentToken := or .Values.agent.token.value .Values.agent.token.existingSecret -}}
{{- $hasApiKey := or .Values.apiKey.value .Values.apiKey.existingSecret -}}
{{- if and $hasApiKey (not $hasCredential) (not $hasAgentToken) -}}
legacy
{{- else -}}
agent
{{- end -}}
{{- end -}}
{{- end }}

{{/*
Credential plumbing.

Which values group supplies the credential depends on the consumer, so the
deprecated `agent.token.*` and `apiKey.*` keep their Secret name and key across
an upgrade, and a forced legacy install never hands the agent token to the
full-account operator.

  "operator", agent mode : credential > agent.token > apiKey
  "operator", legacy mode: apiKey > credential > agent.token
  "agent" (separate legacy agent): agent.token > credential > apiKey

Call with (dict "ctx" . "for" "operator"|"agent").
*/}}
{{- define "hle-operator.credentialSource" -}}
{{- $v := .ctx.Values -}}
{{- $has := dict "credential" (or $v.credential.value $v.credential.existingSecret) "agentToken" (or $v.agent.token.value $v.agent.token.existingSecret) "apiKey" (or $v.apiKey.value $v.apiKey.existingSecret) -}}
{{- $order := list "credential" "agentToken" "apiKey" -}}
{{- if eq .for "agent" -}}
{{- $order = list "agentToken" "credential" "apiKey" -}}
{{- else if eq (include "hle-operator.mode" .ctx) "legacy" -}}
{{- $order = list "apiKey" "credential" "agentToken" -}}
{{- end -}}
{{- $src := "" -}}
{{- range $order -}}
{{- if and (not $src) (get $has .) -}}
{{- $src = . -}}
{{- end -}}
{{- end -}}
{{- $src | default (first $order) -}}
{{- end }}

{{/*
JSON: {name, key, inline, existing} for the chosen source. `inline` is only set
when the chart should create the Secret.
*/}}
{{- define "hle-operator.credentialSpec" -}}
{{- $v := .ctx.Values -}}
{{- $full := include "hle-operator.fullname" .ctx -}}
{{- $src := include "hle-operator.credentialSource" . -}}
{{- $o := dict -}}
{{- if eq $src "agentToken" -}}
{{- $o = dict "existing" $v.agent.token.existingSecret "value" $v.agent.token.value "key" (default "agent-token" $v.agent.token.secretKey) "generated" (printf "%s-agent-token" $full) -}}
{{- else if eq $src "apiKey" -}}
{{- $o = dict "existing" $v.apiKey.existingSecret "value" $v.apiKey.value "key" (default "api-key" $v.apiKey.secretKey) "generated" (printf "%s-api-key" $full) -}}
{{- else -}}
{{- $o = dict "existing" $v.credential.existingSecret "value" $v.credential.value "key" (default "credential" $v.credential.secretKey) "generated" (printf "%s-credential" $full) -}}
{{- end -}}
{{- $inline := ternary "" $o.value (not (empty $o.existing)) -}}
{{- dict "name" (default $o.generated $o.existing) "key" $o.key "inline" $inline "existing" (default "" $o.existing) | toJson -}}
{{- end }}

{{/*
Whether the GitOps reconciler is on: "true" or empty. `operator.enabled=false`
meant "no operator", so an old agent-only values file (operator off, agent on)
stays a pure agent after the merge instead of growing CRD/Ingress RBAC.
*/}}
{{- define "hle-operator.gitops" -}}
{{- if and .Values.gitops.enabled .Values.operator.enabled -}}true{{- end -}}
{{- end }}

{{/*
Namespaced RBAC for an agent-mode pod. Services/Endpoints for discovery, the
GitOps resources when enabled, and get-only Secrets for visitor basic auth.

Never list/watch Secrets, never pods/exec, never pods/log.
*/}}
{{- define "hle-operator.agentNamespaceRules" -}}
{{- if .Values.agent.discovery.enabled }}
- apiGroups: [""]
  resources: ["services", "endpoints"]
  verbs: ["get", "list", "watch"]
{{- end }}
{{- if (include "hle-operator.gitops" .) }}
- apiGroups: ["hle.world"]
  resources: ["hletunnels"]
  verbs: ["get", "list", "watch", "patch"]
- apiGroups: ["hle.world"]
  resources: ["hletunnels/status"]
  verbs: ["patch", "update"]
- apiGroups: ["networking.k8s.io"]
  resources: ["ingresses"]
  # `patch` for the hle.world/public-url annotation the operator writes back
  # (a metadata patch is not covered by ingresses/status).
  verbs: ["get", "list", "watch", "patch"]
- apiGroups: ["networking.k8s.io"]
  resources: ["ingresses/status"]
  verbs: ["patch", "update"]
- apiGroups: [""]
  resources: ["events"]
  verbs: ["create"]
{{- if .Values.gitops.secretResourceNames }}
- apiGroups: [""]
  resources: ["secrets"]
  resourceNames:
    {{- toYaml .Values.gitops.secretResourceNames | nindent 4 }}
  verbs: ["get"]
{{- else if .Values.gitops.clusterWideSecretGet }}
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get"]
{{- end }}
{{- end }}
{{- end }}

{{/*
Cluster-scoped RBAC an agent-mode pod needs. Kopf reads the CRDs it watches,
and watches namespaces cluster-wide. In a scoped install only the CRD grant is
needed. The cluster-wide Secret get, when opted into and scoped, lives here
because a Role cannot grant it.
*/}}
{{- define "hle-operator.agentClusterRules" -}}
{{- if (include "hle-operator.gitops" .) }}
- apiGroups: ["apiextensions.k8s.io"]
  resources: ["customresourcedefinitions"]
  verbs: ["list", "watch"]
{{- if not .Values.scope.namespaces }}
- apiGroups: [""]
  resources: ["namespaces"]
  verbs: ["list", "watch"]
{{- end }}
{{- end }}
{{- if and (include "hle-operator.gitops" .) .Values.scope.namespaces .Values.gitops.clusterWideSecretGet (not .Values.gitops.secretResourceNames) }}
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get"]
{{- end }}
{{- end }}
