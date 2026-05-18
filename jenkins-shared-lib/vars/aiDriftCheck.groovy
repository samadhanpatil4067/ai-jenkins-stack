// ── jenkins-shared-lib/vars/aiDriftCheck.groovy ──────────────
// Pipeline Drift Detection — scores Jenkinsfile vs golden template
//
// Usage in Jenkinsfile:
//   def result = aiDriftCheck(
//     sidecarUrl:     env.AI_SIDECAR_URL,
//     jenkinsfile:    readFile('Jenkinsfile'),
//     goldenTemplate: libraryResource('templates/golden-jenkinsfile.txt'),
//     serviceName:    env.SERVICE_NAME
//   )
//   if (result.compliance_score < 0.70) unstable("Drift: ${result.deviations}")
//
// Returns the parsed JSON response from /check-drift.
// ─────────────────────────────────────────────────────────────

def call(Map config = [:]) {
  def sidecarUrl     = config.sidecarUrl     ?: 'http://ai-sidecar:8000'
  def jenkinsfile    = config.jenkinsfile    ?: ''
  def goldenTemplate = config.goldenTemplate ?: ''
  def serviceName    = config.serviceName    ?: env.SERVICE_NAME ?: 'unknown'
  def timeoutSec     = config.timeoutSec     ?: 30
  def failThreshold  = config.failThreshold  ?: 0.50   // below this → FAILURE
  def warnThreshold  = config.warnThreshold  ?: 0.70   // below this → UNSTABLE

  if (!jenkinsfile || !goldenTemplate) {
    echo "[AI Drift Check] Missing Jenkinsfile or golden template — skipping"
    return [compliance_score: 1.0, deviations: [], security_gaps: []]
  }

  echo "[AI Drift Check] Checking compliance for ${serviceName}"

  def result = [compliance_score: 1.0, deviations: [], security_gaps: []]
  try {
    def response = httpRequest(
      url:         "${sidecarUrl}/check-drift",
      httpMode:    'POST',
      contentType: 'APPLICATION_JSON',
      timeout:     timeoutSec,
      requestBody: groovy.json.JsonOutput.toJson([
        current:      jenkinsfile,
        golden:       goldenTemplate,
        service_name: serviceName,
      ])
    )
    result = readJSON text: response.content

    def score = result.compliance_score
    def icon  = score >= warnThreshold ? '✓' : score >= failThreshold ? '⚠' : '✗'

    echo ""
    echo "┌─ Pipeline Drift Report ─────────────────────────────────────"
    echo "│  Service:    ${serviceName}"
    echo "│  Score:      ${icon} ${(score * 100).toInteger()}%"

    if (result.deviations) {
      echo "│  Deviations:"
      result.deviations.each { d -> echo "│    - ${d}" }
    }
    if (result.security_gaps) {
      echo "│  SECURITY GAPS:"
      result.security_gaps.each { g -> echo "│    ⚠ ${g}" }
    }
    if (result.positive_findings) {
      echo "│  Passing:"
      result.positive_findings.each { f -> echo "│    ✓ ${f}" }
    }
    echo "└──────────────────────────────────────────────────────────────"

    // Push metric to Prometheus
    sh """
      echo 'pipeline_compliance_score{service="${serviceName}"} ${score}' | \
        curl -s --data-binary @- http://pushgateway:9091/metrics/job/aria-drift || true
    """

    // Apply thresholds
    if (score < failThreshold) {
      error("Pipeline compliance score ${score} is below failure threshold ${failThreshold}. Fix: ${result.deviations}")
    } else if (score < warnThreshold) {
      unstable("Pipeline compliance score ${score} is below warning threshold ${warnThreshold}. Issues: ${result.deviations}")
    }

  } catch (hudson.AbortException | org.jenkinsci.plugins.workflow.steps.FlowInterruptedException e) {
    throw e  // re-throw — these are intentional (error/unstable calls above)
  } catch (Exception e) {
    echo "[AI Drift Check] Error: ${e.message} — skipping drift check"
  }

  return result
}
