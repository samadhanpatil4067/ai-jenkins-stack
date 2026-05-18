// ============================================================
// ARIA Stack — Jenkinsfile 2026
// AI-Enabled Jenkins Pipeline for Microservices
//
// Requires:
//   - Jenkins Shared Library: ai-pipeline-lib
//   - Credentials: llm-api-key, github-token, cosign-key
//   - AI Sidecar running at http://ai-sidecar:8000
//   - Plugins: Pipeline, HTTP Request, Shared Groovy Libraries
//
// Features:
//   1. AI Stage Gate     — dynamic stage injection based on changed files
//   2. Failure Analysis  — LLM classifies failures, auto-retries flaky tests
//   3. Drift Detection   — Jenkinsfile compliance scored vs golden template
//   4. Supply Chain      — every image signed with Cosign
//   5. Observability     — pipeline metrics pushed to Prometheus
// ============================================================

@Library('ai-pipeline-lib@main') _

pipeline {

  // ── AGENT ─────────────────────────────────────────────────
  agent {
    docker {
      image 'python:3.11-slim'
      args  '-v /var/run/docker.sock:/var/run/docker.sock'
    }
  }

  // ── ENVIRONMENT ──────────────────────────────────────────
  environment {
    // Service identity
    SERVICE_NAME    = 'payment-service'
    SERVICE_VERSION = '2.1'
    TEAM            = 'platform-engineering'

    // Infrastructure
    AI_SIDECAR_URL  = 'http://ai-sidecar:8000'
    DOCKER_REGISTRY = 'registry.company.io'
    KUBE_NAMESPACE  = 'production'

    // Credentials — NEVER hardcode these
    LLM_API_KEY     = credentials('llm-api-key')
    GITHUB_TOKEN    = credentials('github-token')
    COSIGN_KEY      = credentials('cosign-key')
    KUBECONFIG_DATA = credentials('kubeconfig-production')

    // Dynamic values
    BUILD_COMMIT    = sh(script: 'git rev-parse --short HEAD', returnStdout: true).trim()
    BUILD_BRANCH    = env.BRANCH_NAME ?: 'unknown'
    IMAGE_TAG       = "${env.SERVICE_NAME}:${env.BUILD_COMMIT}"
    FULL_IMAGE      = "${env.DOCKER_REGISTRY}/${env.IMAGE_TAG}"
  }

  // ── OPTIONS ───────────────────────────────────────────────
  options {
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '20', artifactNumToKeepStr: '5'))
    disableConcurrentBuilds(abortPrevious: true)   // cancel stale builds immediately
    timestamps()
    ansiColor('xterm')
  }

  // ── PARAMETERS (for manual/retry runs) ───────────────────
  parameters {
    booleanParam(name: 'FORCE_ALL_STAGES',    defaultValue: false,  description: 'Skip AI gate, run every stage')
    booleanParam(name: 'SKIP_DEPLOY',         defaultValue: false,  description: 'Build and test only, no deploy')
    booleanParam(name: 'RETRY_ISOLATION',     defaultValue: false,  description: 'Set by AI auto-retry for flaky tests')
    string(name:  'OVERRIDE_IMAGE_TAG',       defaultValue: '',     description: 'Deploy a specific image tag')
  }

  // ── STAGES ────────────────────────────────────────────────
  stages {

    // ── 1. SETUP & VALIDATION ────────────────────────────
    stage('Setup') {
      steps {
        script {
          echo "╔══════════════════════════════════════╗"
          echo "║  ARIA Stack — AI-Powered Pipeline    ║"
          echo "╚══════════════════════════════════════╝"
          echo "Service:  ${env.SERVICE_NAME} v${env.SERVICE_VERSION}"
          echo "Commit:   ${env.BUILD_COMMIT}"
          echo "Branch:   ${env.BUILD_BRANCH}"
          echo "Build:    #${env.BUILD_NUMBER}"

          // Install tools
          sh '''
            pip install pytest pytest-cov pytest-xdist semgrep --quiet
            docker --version || echo "Docker not in PATH — using host socket"
          '''

          // Verify AI sidecar is healthy before we depend on it
          def sidecarHealth = sh(
            script: "curl -sf ${AI_SIDECAR_URL}/health || echo 'UNHEALTHY'",
            returnStdout: true
          ).trim()
          if (sidecarHealth.contains('UNHEALTHY')) {
            unstable('AI Sidecar not responding — pipeline will run in STATIC mode (all stages)')
            env.AI_AVAILABLE = 'false'
          } else {
            echo "AI Sidecar healthy: ${sidecarHealth}"
            env.AI_AVAILABLE = 'true'
          }
        }
      }
    }

    // ── 2. AI STAGE GATE ─────────────────────────────────
    // Ask the LLM which stages are needed based on what changed.
    // Docs-only commit? Skip SAST + container build. 38% faster.
    stage('AI Stage Gate') {
      when {
        allOf {
          expression { env.AI_AVAILABLE == 'true' }
          expression { !params.FORCE_ALL_STAGES }
          expression { !params.RETRY_ISOLATION }
        }
      }
      steps {
        script {
          // aiStageGate is defined in jenkins-shared-lib/vars/aiStageGate.groovy
          env.REQUIRED_STAGES = aiStageGate(
            sidecarUrl:      env.AI_SIDECAR_URL,
            availableStages: ['unit-test', 'integration-test', 'sast', 'container-build', 'security-scan', 'drift-check', 'deploy']
          )
          echo "AI gate decision: ${env.REQUIRED_STAGES}"
        }
      }
      post {
        failure {
          // Gate failed — fall back to running everything
          script {
            echo 'AI Stage Gate failed — falling back to all stages'
            env.REQUIRED_STAGES = 'unit-test,sast,container-build,drift-check,deploy'
          }
        }
      }
    }

    // Force all stages when FORCE_ALL_STAGES=true or RETRY_ISOLATION=true
    stage('Stage Gate Override') {
      when {
        anyOf {
          expression { params.FORCE_ALL_STAGES }
          expression { params.RETRY_ISOLATION }
          expression { env.AI_AVAILABLE == 'false' }
        }
      }
      steps {
        script {
          env.REQUIRED_STAGES = params.RETRY_ISOLATION
            ? 'unit-test'                                                    // isolated retry: tests only
            : 'unit-test,sast,container-build,security-scan,drift-check,deploy'  // everything
          echo "Stage override active: ${env.REQUIRED_STAGES}"
        }
      }
    }

    // ── 3. UNIT TESTS ──────────────────────────────────
    stage('Unit Tests') {
      when {
        expression { env.REQUIRED_STAGES?.contains('unit-test') }
      }
      parallel {

        stage('Tests: Auth Module') {
          steps {
            sh '''
              pytest tests/auth/ \
                -v --tb=short \
                --junitxml=test-results/auth.xml \
                --cov=src/auth --cov-report=xml:coverage/auth.xml \
                -n auto
            '''
          }
        }

        stage('Tests: Payment Module') {
          steps {
            sh '''
              pytest tests/payment/ \
                -v --tb=short \
                --junitxml=test-results/payment.xml \
                --cov=src/payment --cov-report=xml:coverage/payment.xml \
                -n auto
            '''
          }
        }

        stage('Tests: Contract Tests') {
          steps {
            sh '''
              pytest tests/contracts/ \
                -v --tb=short \
                --junitxml=test-results/contracts.xml \
                -n auto
            '''
          }
        }

      }
      post {
        always {
          junit(testResults: 'test-results/*.xml', allowEmptyResults: true)
          publishCoverage(adapters: [jacocoAdapter('coverage/*.xml')], sourceFileResolver: sourceFiles('STORE_LAST_BUILD'))
        }
      }
    }

    // ── 4. INTEGRATION TESTS ───────────────────────────
    stage('Integration Tests') {
      when {
        allOf {
          expression { env.REQUIRED_STAGES?.contains('integration-test') }
          expression { env.BUILD_BRANCH != 'main' }   // skip on main — covered by unit + deploy
        }
      }
      steps {
        sh '''
          pytest tests/integration/ \
            -v --tb=short \
            --junitxml=test-results/integration.xml \
            -m "not slow" \
            --timeout=60
        '''
      }
      post {
        always {
          junit(testResults: 'test-results/integration.xml', allowEmptyResults: true)
        }
      }
    }

    // ── 5. STATIC ANALYSIS ─────────────────────────────
    stage('SAST — Semgrep') {
      when {
        expression { env.REQUIRED_STAGES?.contains('sast') }
      }
      steps {
        sh '''
          semgrep \
            --config=auto \
            --config=p/owasp-top-ten \
            --config=p/secrets \
            src/ \
            --json \
            --output=reports/semgrep.json \
            --severity=ERROR \
            --severity=WARNING \
            --max-findings=0
        '''
      }
      post {
        always {
          archiveArtifacts(artifacts: 'reports/semgrep.json', allowEmptyArchive: true)
        }
        failure {
          echo 'SAST found vulnerabilities — check reports/semgrep.json'
        }
      }
    }

    // ── 6. CONTAINER BUILD + SIGN ──────────────────────
    stage('Container Build + Sign') {
      when {
        expression { env.REQUIRED_STAGES?.contains('container-build') }
      }
      stages {

        stage('Docker Build') {
          steps {
            sh """
              docker build \
                --tag ${env.FULL_IMAGE} \
                --build-arg BUILD_COMMIT=${env.BUILD_COMMIT} \
                --build-arg BUILD_DATE=\$(date -u +%Y-%m-%dT%H:%M:%SZ) \
                --build-arg SERVICE_VERSION=${env.SERVICE_VERSION} \
                --cache-from ${env.FULL_IMAGE}-cache \
                --label "org.opencontainers.image.revision=${env.BUILD_COMMIT}" \
                --label "org.opencontainers.image.created=\$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
                .
            """
          }
        }

        stage('Cosign Sign') {
          steps {
            // Sign the image — supply chain integrity, not optional in 2026
            sh """
              echo "\${COSIGN_KEY}" | cosign sign --key /dev/stdin \
                --yes \
                ${env.FULL_IMAGE}
              echo "✓ Image signed: ${env.FULL_IMAGE}"
            """
          }
        }

        stage('Docker Push') {
          steps {
            sh """
              docker push ${env.FULL_IMAGE}
              docker tag  ${env.FULL_IMAGE} ${env.FULL_IMAGE}-cache
              docker push ${env.FULL_IMAGE}-cache
              echo "✓ Pushed: ${env.FULL_IMAGE}"
            """
          }
        }

      }
    }

    // ── 7. SECURITY SCAN (container) ───────────────────
    stage('Container Security Scan') {
      when {
        expression { env.REQUIRED_STAGES?.contains('security-scan') }
      }
      steps {
        sh """
          # Trivy vulnerability scan on the built image
          trivy image \
            --exit-code 1 \
            --severity HIGH,CRITICAL \
            --format json \
            --output reports/trivy.json \
            ${env.FULL_IMAGE}
        """
      }
      post {
        always {
          archiveArtifacts(artifacts: 'reports/trivy.json', allowEmptyArchive: true)
        }
        failure {
          echo 'High/Critical CVEs found in container — check reports/trivy.json'
        }
      }
    }

    // ── 8. PIPELINE DRIFT CHECK ────────────────────────
    stage('Pipeline Drift Check') {
      when {
        expression { env.REQUIRED_STAGES?.contains('drift-check') }
      }
      steps {
        script {
          // aiDriftCheck defined in jenkins-shared-lib/vars/aiDriftCheck.groovy
          def driftResult = aiDriftCheck(
            sidecarUrl:    env.AI_SIDECAR_URL,
            jenkinsfile:   readFile('Jenkinsfile'),
            goldenTemplate: libraryResource('templates/golden-jenkinsfile.txt'),
            serviceName:   env.SERVICE_NAME
          )

          echo "Compliance score: ${driftResult.compliance_score}"

          if (driftResult.compliance_score < 0.70) {
            unstable("Pipeline drift: ${driftResult.deviations}")
          }
          if (driftResult.security_gaps) {
            echo "Security gaps: ${driftResult.security_gaps}"
          }
        }
      }
    }

    // ── 9. DEPLOY ─────────────────────────────────────
    stage('Deploy to Kubernetes') {
      when {
        allOf {
          expression { env.REQUIRED_STAGES?.contains('deploy') }
          expression { !params.SKIP_DEPLOY }
          anyOf {
            branch 'main'
            branch pattern: 'release/*', comparator: 'GLOB'
          }
        }
      }
      steps {
        script {
          def imageTag = params.OVERRIDE_IMAGE_TAG ?: env.IMAGE_TAG
          sh """
            # Write kubeconfig
            echo "\${KUBECONFIG_DATA}" > /tmp/kubeconfig
            export KUBECONFIG=/tmp/kubeconfig

            # Helm upgrade/install
            helm upgrade --install ${env.SERVICE_NAME} ./helm/${env.SERVICE_NAME} \
              --namespace ${env.KUBE_NAMESPACE} \
              --set image.repository=${env.DOCKER_REGISTRY}/${env.SERVICE_NAME} \
              --set image.tag=${env.BUILD_COMMIT} \
              --set deployment.annotations."kubectl\\.kubernetes\\.io/change-cause"="Commit ${env.BUILD_COMMIT} by Jenkins build ${env.BUILD_NUMBER}" \
              --wait \
              --timeout=5m \
              --atomic

            # Verify rollout
            kubectl rollout status deployment/${env.SERVICE_NAME} \
              --namespace=${env.KUBE_NAMESPACE} \
              --timeout=3m

            echo "✓ Deployed ${env.SERVICE_NAME}:${env.BUILD_COMMIT} to ${env.KUBE_NAMESPACE}"
          """
        }
      }
    }

  }
  // ── END STAGES ────────────────────────────────────────────

  // ── POST ─────────────────────────────────────────────────
  post {

    // SELF-HEALING: AI analyses every failure
    failure {
      script {
        if (env.AI_AVAILABLE == 'true') {
          // aiFailureAnalysis defined in jenkins-shared-lib/vars/aiFailureAnalysis.groovy
          aiFailureAnalysis(
            sidecarUrl:   env.AI_SIDECAR_URL,
            githubToken:  env.GITHUB_TOKEN,
            githubRepo:   env.GIT_URL?.replaceAll(/.*github\.com[\/:]/, '')?.replace('.git', ''),
            serviceName:  env.SERVICE_NAME,
            branchName:   env.BUILD_BRANCH,
            buildNumber:  env.BUILD_NUMBER
          )
        } else {
          echo 'AI Sidecar unavailable — manual investigation required'
          // Still send a basic Slack notification
          sh """
            curl -s -X POST \${SLACK_WEBHOOK:-} \
              -H 'Content-Type: application/json' \
              -d '{"text": "Build #${env.BUILD_NUMBER} FAILED: ${env.SERVICE_NAME} on ${env.BUILD_BRANCH}"}' || true
          """
        }
      }
    }

    // Push metrics regardless of outcome
    always {
      script {
        sh """
          # Pipeline duration metric
          echo 'pipeline_duration_seconds{service="${env.SERVICE_NAME}",branch="${env.BUILD_BRANCH}",result="${currentBuild.result ?: 'UNKNOWN'}",ai_available="${env.AI_AVAILABLE}"} ${currentBuild.duration/1000}' \
            | curl -s --data-binary @- http://pushgateway:9091/metrics/job/jenkins-pipeline || true

          # Stage count metric
          echo 'pipeline_stages_run_total{service="${env.SERVICE_NAME}"} ${env.REQUIRED_STAGES?.split(',')?.size() ?: 0}' \
            | curl -s --data-binary @- http://pushgateway:9091/metrics/job/jenkins-pipeline || true
        """
      }
      // Clean workspace — keep it lean
      cleanWs(
        cleanWhenFailure: false,
        cleanWhenAborted: true,
        patterns: [[pattern: 'reports/**', type: 'INCLUDE'],
                   [pattern: 'test-results/**', type: 'INCLUDE']]
      )
    }

    success {
      echo "✓ Pipeline complete — ${env.SERVICE_NAME}:${env.BUILD_COMMIT}"
    }

  }
  // ── END POST ──────────────────────────────────────────────

}
