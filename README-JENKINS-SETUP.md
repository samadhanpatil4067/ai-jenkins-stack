# Jenkins Setup Guide — ARIA Stack

Jenkins already running at `http://localhost:8080`. 
This guide wires it to GitHub with the AI pipeline.

## Quick start (automated)

```bash
cp .env.example .env && nano .env   # fill in all values
docker compose up -d                 # start AI sidecar + Prometheus + Grafana
source .env && ./scripts/setup-jenkins.sh  # configure Jenkins automatically
```

Done. Push a commit to your GitHub repo and the pipeline runs.

## What the setup script does

1. Verifies Jenkins connectivity
2. Installs required plugins (GitHub, HTTP Request, Shared Groovy Libraries, etc.)
3. Sets global environment variables (AI_SIDECAR_URL, PUSHGATEWAY_URL, etc.)
4. Adds credentials (llm-api-key, github-token, cosign-key)
5. Registers the `ai-pipeline-lib` shared library
6. Creates a multibranch pipeline job pointed at your GitHub repo
7. Registers a GitHub webhook for push/PR triggers

## Jenkins API token (required)

Jenkins → your username (top right) → Configure → API Token → Add new Token → Generate
Paste into `.env` as `JENKINS_TOKEN`

## Required plugins (auto-installed by script)

- GitHub + GitHub Branch Source
- HTTP Request (for AI Sidecar calls)
- Pipeline: Shared Groovy Libraries
- Git, Timestamper, AnsiColor, Workspace Cleanup

## Global env vars set automatically

| Variable | Default | Override in `.env` |
|----------|---------|-------------------|
| `AI_SIDECAR_URL` | `http://localhost:8000` | Change if sidecar is on different host |
| `PUSHGATEWAY_URL` | `http://localhost:9091` | - |
| `DOCKER_REGISTRY` | `registry.company.io` | Your registry |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Groq/Ollama/Azure |
| `LLM_MODEL` | `gpt-4o` | Any model name |

## GitHub webhook for localhost Jenkins

GitHub cannot reach `localhost` directly. Options:

### Option A — ngrok (recommended)
```bash
ngrok http 8080           # in terminal 1
source .env && ./scripts/setup-github-webhook.sh   # terminal 2 — auto-detects ngrok URL
```

### Option B — pollSCM fallback (already configured)
The Jenkinsfile includes `pollSCM('H/5 * * * *')` — Jenkins polls GitHub every 5 minutes.
No webhook needed for basic operation.

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `Cannot reach Jenkins` | Check JENKINS_URL and JENKINS_TOKEN |
| AI Sidecar connection refused | `docker compose up -d` first; use `http://localhost:8000` not `http://ai-sidecar:8000` |
| `@Library not found` | Setup script registers it; check Configure System → Global Pipeline Libraries |
| Pipeline doesn't auto-trigger | Check webhook in GitHub repo → Settings → Webhooks → Recent Deliveries |
| Credentials not found | Re-run setup script or add manually via Jenkins UI |
