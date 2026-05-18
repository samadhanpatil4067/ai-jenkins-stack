// ── jenkins-shared-lib/vars/aiFailureAnalysis.groovy ─────────
// AI Failure Analysis + Self-Healing
//
// Usage in Jenkinsfile post { failure { ... } }:
//   aiFailureAnalysis(
//     sidecarUrl:  env.AI_SIDECAR_URL,
//     githubToken: env.GITHUB_TOKEN,
//     githubRepo:  'yourorg/payment-service',
//     serviceName: env.SERVICE_NAME,
//     branchName:  env.BUILD_BRANCH,
//     buildNumber: env.BUILD_NUMBER
//   )
//
// What it does:
//   1. Collects last 50 log lines + stack trace
//   2. Sends to AI sidecar for classification
//   3. If FLAKY_TEST + confidence > 0.82 → schedules auto-retry
//   4. Files a GitHub issue with diagnosis
//   5. Pushes failure metrics to Prometheus
// ─────────────────────────────────────────────────────────────

def call(Map config = [:]) {
  def sidecarUrl  = config.sidecarUrl  ?: 'http://ai-sidecar:8000'
  def githubToken = config.githubToken ?: env.GITHUB_TOKEN
  def githubRepo  = config.githubRepo  ?: ''
  def serviceName = config.serviceName ?: env.SERVICE_NAME ?: 'unknown'
  def branchName  = config.branchName  ?: env.BRANCH_NAME  ?: 'unknown'
  def buildNumber = config.buildNumber ?: env.BUILD_NUMBER  ?: '0'
  def timeoutSec  = config.timeoutSec  ?: 60
  def retryConfidenceThreshold = config.retryThreshold ?: 0.82

  echo "[AI Failure Analysis] Starting — collecting build evidence"

  // ── Collect evidence ─────────────────────────────────────
  def logContent = ''
  try {
    logContent = currentBuild.rawBuild.getLog(50).join('\n')
  } catch (Exception e) {
    logContent = "Could not collect logs: ${e.message}"
  }

  def stackTrace = ''
  try {
    stackTrace = sh(
      script: 'cat test-results/*.xml 2>/dev/null | head -200 || echo "no XML results"',
      returnStdout: true
    ).trim()
  } catch (Exception ignored) {
    stackTrace = 'No test result files found'
  }

  // ── Call AI Sidecar ──────────────────────────────────────
  def diagnosis = null
  try {
    def response = httpRequest(
      url:         "${sidecarUrl}/analyze-failure",
      httpMode:    'POST',
      contentType: 'APPLICATION_JSON',
      timeout:     timeoutSec,
      requestBody: groovy.json.JsonOutput.toJson([
        stack_trace:  stackTrace,
        log_tail:     logContent,
        service:      serviceName,
        branch:       branchName,
        build_number: buildNumber,
      ])
    )
    diagnosis = readJSON text: response.content

    echo ""
    echo "┌─ AI Failure Diagnosis ───────────────────────────────────────"
    echo "│  Type:       ${diagnosis.failure_type}  (confidence: ${diagnosis.confidence})"
    echo "│  Root cause: ${diagnosis.root_cause}"
    echo "│  Fix:        ${diagnosis.suggested_fix}"
    echo "│  Severity:   ${diagnosis.severity}"
    echo "│  Auto-retry: ${diagnosis.should_auto_retry}"
    echo "└──────────────────────────────────────────────────────────────"

  } catch (Exception e) {
    echo "[AI Failure Analysis] Could not reach AI sidecar: ${e.message}"
    echo "[AI Failure Analysis] Filing generic failure issue"
    _fileGenericGithubIssue(githubToken, githubRepo, serviceName, buildNumber)
    return
  }

  // ── Auto-retry if flaky ───────────────────────────────────
  if (diagnosis.failure_type == 'FLAKY_TEST' && diagnosis.confidence > retryConfidenceThreshold) {
    echo "[AI Failure Analysis] Flaky test detected at ${diagnosis.confidence} confidence — scheduling isolated retry"
    try {
      build(
        job:        env.JOB_NAME,
        wait:       false,
        parameters: [booleanParam(name: 'RETRY_ISOLATION', value: true)],
      )
      echo "[AI Failure Analysis] ✓ Isolated retry scheduled"
    } catch (Exception e) {
      echo "[AI Failure Analysis] Could not schedule retry: ${e.message}"
    }
  }

  // ── File GitHub issue ─────────────────────────────────────
  if (githubRepo && githubToken) {
    _fileGitHubIssue(
      githubToken: githubToken,
      githubRepo:  githubRepo,
      diagnosis:   diagnosis,
      serviceName: serviceName,
      buildNumber: buildNumber,
      branchName:  branchName,
    )
  } else {
    echo "[AI Failure Analysis] GitHub issue filing skipped — no token or repo configured"
  }

  // ── Push metrics to Prometheus ────────────────────────────
  sh """
    echo 'build_failure_type{service="${serviceName}",type="${diagnosis.failure_type}",branch="${branchName}"} 1' | \
      curl -s --data-binary @- http://pushgateway:9091/metrics/job/aria-failures || true

    echo 'ai_diagnosis_confidence{service="${serviceName}",type="${diagnosis.failure_type}"} ${diagnosis.confidence}' | \
      curl -s --data-binary @- http://pushgateway:9091/metrics/job/aria-failures || true
  """
}

// ── Private: file GitHub issue ───────────────────────────────
def _fileGitHubIssue(Map p) {
  def title = "[CI] ${p.diagnosis.failure_type}: ${p.serviceName} build #${p.buildNumber}"
  def body  = """## AI Failure Diagnosis

**Service:**      `${p.serviceName}`
**Branch:**       `${p.branchName}`
**Build:**        #${p.buildNumber}
**Failure type:** `${p.diagnosis.failure_type}` (confidence: ${p.diagnosis.confidence})
**Severity:**     ${p.diagnosis.severity}

### Root cause
${p.diagnosis.root_cause}

### Suggested fix
${p.diagnosis.suggested_fix}

---
*Filed automatically by ARIA AI Sidecar · Build #${p.buildNumber}*
"""
  def labels = [p.diagnosis.github_label, 'ci-automated', "severity-${p.diagnosis.severity?.toLowerCase() ?: 'unknown'}"]

  try {
    sh """
      curl -s -o /dev/null -w "%{http_code}" \
        -X POST https://api.github.com/repos/${p.githubRepo}/issues \
        -H 'Authorization: token ${p.githubToken}' \
        -H 'Content-Type: application/json' \
        -d '${groovy.json.JsonOutput.toJson([title: title, body: body, labels: labels])}'
    """
    echo "[AI Failure Analysis] ✓ GitHub issue filed: ${title}"
  } catch (Exception e) {
    echo "[AI Failure Analysis] Could not file GitHub issue: ${e.message}"
  }
}

def _fileGenericGithubIssue(token, repo, service, build) {
  if (!token || !repo) return
  sh """
    curl -s -o /dev/null \
      -X POST https://api.github.com/repos/${repo}/issues \
      -H 'Authorization: token ${token}' \
      -H 'Content-Type: application/json' \
      -d '{"title":"[CI] Build failed: ${service} #${build}","labels":["ci-failure","needs-investigation"]}'
  """
}
