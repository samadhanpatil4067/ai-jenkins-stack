#!/usr/bin/env bash
# ARIA Stack — Jenkins Auto-Setup Script
# Reads all config from .env — run: source .env && ./scripts/setup-jenkins.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "${SCRIPT_DIR}/../.env" ]] && { set -a; source "${SCRIPT_DIR}/../.env"; set +a; }

: "${JENKINS_URL:?  Set JENKINS_URL in .env (e.g. http://localhost:8080)}"
: "${JENKINS_USER:? Set JENKINS_USER in .env}"
: "${JENKINS_TOKEN:?Set JENKINS_TOKEN in .env — generate at: Jenkins → username → Configure → API Token}"
: "${GITHUB_TOKEN:? Set GITHUB_TOKEN in .env}"
: "${LLM_API_KEY:?  Set LLM_API_KEY in .env}"
: "${GITHUB_REPO_OWNER:? Set GITHUB_REPO_OWNER in .env}"
: "${GITHUB_REPO_NAME:?  Set GITHUB_REPO_NAME in .env}"

J="${JENKINS_URL%/}"
AUTH="${JENKINS_USER}:${JENKINS_TOKEN}"
AI_URL="${AI_SIDECAR_URL:-http://localhost:8000}"
PGW_URL="${PUSHGATEWAY_URL:-http://localhost:9091}"
LLM_BASE="${LLM_BASE_URL:-https://api.openai.com/v1}"
LLM_MDL="${LLM_MODEL:-gpt-4o}"
DOCKER_REG="${DOCKER_REGISTRY:-registry.company.io}"
KUBE_NS="${KUBE_NAMESPACE:-production}"
REPO_URL="${GITHUB_REPO_URL:-https://github.com/${GITHUB_REPO_OWNER}/${GITHUB_REPO_NAME}.git}"

echo ""; echo "╔══════════════════════════════════╗"
echo "║  ARIA Stack — Jenkins Setup      ║"; echo "╚══════════════════════════════════╝"
echo "Jenkins:  $J"; echo "GitHub:   ${GITHUB_REPO_OWNER}/${GITHUB_REPO_NAME}"
echo "Sidecar:  $AI_URL"; echo ""

# Verify connectivity
echo "Step 1/6 — Verifying Jenkins..."
curl -sf -u "$AUTH" "$J/api/json" >/dev/null || \
  { echo "ERROR: Cannot reach $J — check JENKINS_URL and JENKINS_TOKEN"; exit 1; }
echo "  ✓ Connected"

# Get CSRF crumb
CRUMB=$(curl -sf -u "$AUTH" "$J/crumbIssuer/api/json" 2>/dev/null \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['crumbRequestField']+': '+d['crumb'])" \
  2>/dev/null || echo "Jenkins-Crumb: skip")

# Install plugins
echo ""; echo "Step 2/6 — Installing plugins..."
for p in github github-branch-source pipeline-github workflow-aggregator \
          workflow-multibranch http_request pipeline-groovy-lib git \
          credentials-binding timestamper ansicolor ws-cleanup junit; do
  curl -sf -u "$AUTH" -X POST "$J/pluginManager/installNecessaryPlugins" \
    -H "Content-Type: application/xml" -H "$CRUMB" \
    -d "<jenkins><install plugin=\"${p}@latest\"/></jenkins>" 2>/dev/null || true
  printf "  %s\n" "$p"
done
echo "  ✓ Plugin requests sent"

# Set global env vars via Groovy script console
echo ""; echo "Step 3/6 — Setting global environment variables..."
GROOVY="
import jenkins.model.Jenkins
import hudson.slaves.EnvironmentVariablesNodeProperty
def j = Jenkins.instance
def np = j.globalNodeProperties
def evl = np.getAll(EnvironmentVariablesNodeProperty)
def ev = evl ? evl[0] : new EnvironmentVariablesNodeProperty()
if (!evl) np.add(ev)
def e = ev.envVars
e.put('AI_SIDECAR_URL','${AI_URL}')
e.put('PUSHGATEWAY_URL','${PGW_URL}')
e.put('DOCKER_REGISTRY','${DOCKER_REG}')
e.put('KUBE_NAMESPACE','${KUBE_NS}')
e.put('LLM_BASE_URL','${LLM_BASE}')
e.put('LLM_MODEL','${LLM_MDL}')
j.save(); println 'done'
"
curl -sf -u "$AUTH" -X POST "$J/scriptText" -H "$CRUMB" \
  --data-urlencode "script=$GROOVY" | grep -v '^$' || true
echo "  ✓ Env vars set"

# Add credentials
echo ""; echo "Step 4/6 — Adding credentials..."
add_cred() {
  local id="$1" secret="$2" desc="$3"
  curl -sf -u "$AUTH" -X POST \
    "$J/credentials/store/system/domain/_/createCredentials" \
    -H "Content-Type: application/xml" -H "$CRUMB" \
    -d "<org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl>
          <scope>GLOBAL</scope><id>${id}</id>
          <description>${desc}</description><secret>${secret}</secret>
        </org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl>" 2>/dev/null \
    && echo "  ✓ $id" || echo "  ⚠ $id already exists (update manually if needed)"
}
add_cred "llm-api-key"  "${LLM_API_KEY}"   "ARIA: LLM API key"
add_cred "github-token" "${GITHUB_TOKEN}"  "ARIA: GitHub token"
[[ -n "${COSIGN_KEY:-}" ]] && add_cred "cosign-key" "${COSIGN_KEY}" "ARIA: Cosign private key" \
  || echo "  ⚠ cosign-key skipped (add manually: cosign generate-key-pair)"

# Register shared library
echo ""; echo "Step 5/6 — Registering ai-pipeline-lib..."
GROOVY_LIB="
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.libs.*
import jenkins.plugins.git.GitSCMSource
def src = new GitSCMSource('${REPO_URL}')
src.credentialsId = 'github-token'
def lib = new LibraryConfiguration('ai-pipeline-lib', new SCMSourceRetriever(src))
lib.defaultVersion = 'main'; lib.implicit = false; lib.allowVersionOverride = true
Jenkins.instance.getDescriptorByType(GlobalLibraries).libraries = [lib]
Jenkins.instance.save(); println 'done'
"
curl -sf -u "$AUTH" -X POST "$J/scriptText" -H "$CRUMB" \
  --data-urlencode "script=$GROOVY_LIB" | grep -v '^$' || true
echo "  ✓ ai-pipeline-lib registered"

# Create multibranch pipeline job
echo ""; echo "Step 6/6 — Creating pipeline job..."
curl -sf -u "$AUTH" -X POST "$J/job/aria-stack/doDelete" -H "$CRUMB" 2>/dev/null || true

JOB_XML="<?xml version=\"1.1\" encoding=\"UTF-8\"?>
<org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject plugin=\"workflow-multibranch\">
  <description>ARIA Stack AI Pipeline</description>
  <sources class=\"jenkins.branch.MultiBranchProject\$BranchSourceList\">
    <data>
      <jenkins.branch.BranchSource>
        <source class=\"org.jenkinsci.plugins.github_branch_source.GitHubSCMSource\">
          <credentialsId>github-token</credentialsId>
          <repoOwner>${GITHUB_REPO_OWNER}</repoOwner>
          <repository>${GITHUB_REPO_NAME}</repository>
          <traits>
            <org.jenkinsci.plugins.github__branch__source.BranchDiscoveryTrait><strategyId>1</strategyId></org.jenkinsci.plugins.github__branch__source.BranchDiscoveryTrait>
            <org.jenkinsci.plugins.github__branch__source.OriginPullRequestDiscoveryTrait><strategyId>1</strategyId></org.jenkinsci.plugins.github__branch__source.OriginPullRequestDiscoveryTrait>
          </traits>
        </source>
        <strategy class=\"jenkins.branch.DefaultBranchPropertyStrategy\"><properties class=\"empty-list\"/></strategy>
      </jenkins.branch.BranchSource>
    </data>
  </sources>
  <factory class=\"org.jenkinsci.plugins.workflow.multibranch.WorkflowBranchProjectFactory\">
    <owner class=\"org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject\" reference=\"../..\"/>
    <scriptPath>Jenkinsfile</scriptPath>
  </factory>
</org.jenkinsci.plugins.workflow.multibranch.WorkflowMultiBranchProject>"

curl -sf -u "$AUTH" -X POST "$J/createItem?name=aria-stack" \
  -H "Content-Type: application/xml" -H "$CRUMB" -d "$JOB_XML"
echo "  ✓ Job created: $J/job/aria-stack"

# GitHub webhook
echo ""
WEBHOOK_URL="$J/github-webhook/"
STATUS=$(curl -sf -o /dev/null -w "%{http_code}" \
  -X POST "https://api.github.com/repos/${GITHUB_REPO_OWNER}/${GITHUB_REPO_NAME}/hooks" \
  -H "Authorization: token ${GITHUB_TOKEN}" -H "Content-Type: application/json" \
  -d "{\"name\":\"web\",\"active\":true,\"events\":[\"push\",\"pull_request\"],
       \"config\":{\"url\":\"${WEBHOOK_URL}\",\"content_type\":\"json\"}}" 2>/dev/null || echo "0")
[[ "$STATUS" == "201" || "$STATUS" == "422" ]] \
  && echo "Webhook: ✓ ${WEBHOOK_URL}" \
  || echo "Webhook: ⚠ HTTP ${STATUS} — if localhost, use ngrok then re-run this script"

echo ""
echo "╔══════════════════════════════════╗"
echo "║  Setup complete!                 ║"
echo "╚══════════════════════════════════╝"
echo "Pipeline: $J/job/aria-stack"
echo "Sidecar:  http://localhost:8000/docs"
echo ""
echo "Push a commit to GitHub → pipeline runs automatically."
