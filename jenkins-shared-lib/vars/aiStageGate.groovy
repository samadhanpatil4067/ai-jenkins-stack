// ── jenkins-shared-lib/vars/aiStageGate.groovy ───────────────
// Dynamic Stage Gate: ask the AI which stages to run.
//
// Usage in Jenkinsfile:
//   env.REQUIRED_STAGES = aiStageGate(
//     sidecarUrl:      env.AI_SIDECAR_URL,
//     availableStages: ['unit-test', 'sast', 'container-build', 'drift-check', 'deploy']
//   )
//
// Returns a comma-delimited string of required stage names.
// On any error, returns ALL stages (safe default).
// ─────────────────────────────────────────────────────────────

def call(Map config = [:]) {
  def sidecarUrl      = config.sidecarUrl      ?: 'http://ai-sidecar:8000'
  def availableStages = config.availableStages ?: ['unit-test', 'sast', 'container-build', 'drift-check']
  def timeoutSec      = config.timeoutSec      ?: 30
  def service         = env.SERVICE_NAME       ?: 'unknown'
  def branch          = env.BUILD_BRANCH       ?: env.BRANCH_NAME ?: 'unknown'

  echo "[AI Stage Gate] Analysing changed files for ${service} on ${branch}"

  // ── Get changed files ────────────────────────────────────
  def changedFiles = []
  try {
    def raw = sh(
      script: 'git diff --name-only HEAD~1 HEAD 2>/dev/null || git diff --name-only HEAD',
      returnStdout: true
    ).trim()
    changedFiles = raw ? raw.split('\n').toList() : []
    echo "[AI Stage Gate] Changed files (${changedFiles.size()}): ${changedFiles.take(10).join(', ')}"
  } catch (Exception e) {
    echo "[AI Stage Gate] Could not get changed files: ${e.message} — running all stages"
    return availableStages.join(',')
  }

  if (changedFiles.isEmpty()) {
    echo "[AI Stage Gate] No changed files detected — running all stages"
    return availableStages.join(',')
  }

  // ── Call AI Sidecar ──────────────────────────────────────
  def requiredStages = availableStages  // safe default
  try {
    def response = httpRequest(
      url:          "${sidecarUrl}/check-stage-relevance",
      httpMode:     'POST',
      contentType:  'APPLICATION_JSON',
      timeout:      timeoutSec,
      requestBody:  groovy.json.JsonOutput.toJson([
        changed_files:    changedFiles,
        available_stages: availableStages,
        service:          service,
        branch:           branch,
      ])
    )

    def parsed = readJSON text: response.content
    requiredStages = parsed.required_stages ?: availableStages

    // Log the AI's decision
    echo "[AI Stage Gate] ✓ Required:  ${requiredStages.join(', ')}"
    if (parsed.skipped_stages) {
      echo "[AI Stage Gate] ⊘ Skipped:   ${parsed.skipped_stages.join(', ')}"
    }
    if (parsed.estimated_savings_pct) {
      echo "[AI Stage Gate] ⚡ Estimated ${parsed.estimated_savings_pct}% faster than full pipeline"
    }

    // Push stage count metric to Prometheus
    sh """
      echo 'pipeline_stages_injected{service="${service}",branch="${branch}"} ${requiredStages.size()}' | \
        curl -s --data-binary @- http://pushgateway:9091/metrics/job/aria-stage-gate || true
    """

  } catch (Exception e) {
    echo "[AI Stage Gate] Error: ${e.message} — falling back to all stages"
    return availableStages.join(',')
  }

  return requiredStages.join(',')
}
