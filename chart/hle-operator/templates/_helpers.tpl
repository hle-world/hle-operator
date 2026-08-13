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
