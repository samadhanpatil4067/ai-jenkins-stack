// ARIA Stack — Jenkinsfile v2
// Fixed: agent any (not docker), BUILD_BRANCH quoted correctly

@Library('ai-pipeline-lib@main') _

pipeline {

  agent any   // ← uses your existing Jenkins, no Docker agent needed

  environment {
    SERVICE_NAME     = 'payment-service'
    SERVICE_VERSION  = '2.1'
    TEAM             = 'platform-engineering'

    AI_SIDECAR_URL   = "${env.AI_SIDECAR_URL   ?: 'http://localhost:8000'}"
    PUSHGATEWAY_URL  = "${env.PUSHGATEWAY_URL   ?: 'http://localhost:9091'}"
    DOCKER_REGISTRY  = "${env.DOCKER_REGISTRY   ?: 'registry.company.io'}"
    KUBE_NAMESPACE   = "${env.KUBE_NAMESPACE    ?: 'production'}"

    LLM_API_KEY      = credentials('llm-api-key')
    GITHUB_TOKEN     = credentials('github-token')

    BUILD_COMMIT     = sh(script: 'git rev-parse --short HEAD', returnStdout: true).trim()
    BUILD_BRANCH     = "${env.BRANCH_NAME ?: env.GIT_BRANCH ?: 'unknown'}"  // ← quoted = valid
    GITHUB_REPO      = "${env.GIT_URL?.replaceAll(/.*github\\.com[\\/:]/, '')?.replace('.git', '') ?: ''}"
    IMAGE_TAG        = "${env.SERVICE_NAME}:${env.BUILD_COMMIT}"
    FULL_IMAGE       = "${env.DOCKER_REGISTRY}/${env.IMAGE_TAG}"
  }

  options {
    timeout(time: 30, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '20'))
    disableConcurrentBuilds(abortPrevious: true)
    timestamps()
  }

  triggers {
    githubPush()
    pollSCM('H/5 * * * *')
  }

  parameters {
    booleanParam(name: 'FORCE_ALL_STAGES', defaultValue: false, description: 'Skip AI gate — run all stages')
    booleanParam(name: 'SKIP_DEPLOY',      defaultValue: true,  description: 'Skip k8s deploy (default on for local)')
    booleanParam(name: 'RETRY_ISOLATION',  defaultValue: false, description: 'Set by AI auto-retry')
  }

  stages {

    stage('Setup') {
      steps {
        script {
          echo "╔══════════════════════════════════════╗"
          echo "║  ARIA Stack — AI-Powered Pipeline    ║"
          echo "╚══════════════════════════════════════╝"
          echo "Service: ${env.SERVICE_NAME}  Branch: ${env.BUILD_BRANCH}  Build: #${env.BUILD_NUMBER}"
          echo "AI Sidecar: ${env.AI_SIDECAR_URL}"

          sh 'mkdir -p test-results coverage reports'

          def health = sh(
            script: "curl -sf --connect-timeout 5 ${env.AI_SIDECAR_URL}/health || echo 'UNHEALTHY'",
            returnStdout: true
          ).trim()
          env.AI_AVAILABLE = health.contains('UNHEALTHY') ? 'false' : 'true'
          echo "AI Sidecar: ${env.AI_AVAILABLE == 'true' ? health : 'UNAVAILABLE — running static mode'}"
        }
      }
    }

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
          env.REQUIRED_STAGES = aiStageGate(
            sidecarUrl:      env.AI_SIDECAR_URL,
            availableStages: ['unit-test', 'sast', 'container-build', 'drift-check']
          )
          echo "AI gate: ${env.REQUIRED_STAGES}"
        }
      }
      post {
        failure {
          script {
            env.REQUIRED_STAGES = 'unit-test,drift-check'
            echo 'AI gate failed — fallback stages: unit-test, drift-check'
          }
        }
      }
    }

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
            ? 'unit-test'
            : 'unit-test,sast,drift-check'
          echo "Override: ${env.REQUIRED_STAGES}"
        }
      }
    }

    stage('Unit Tests') {
      when { expression { env.REQUIRED_STAGES?.contains('unit-test') } }
      steps {
        sh '''
          python3 -m pytest tests/payment/ \
            -v --tb=short \
            --junitxml=test-results/payment.xml \
            2>&1 || true
        '''
      }
      post {
        always {
          junit(testResults: 'test-results/*.xml', allowEmptyResults: true)
        }
      }
    }

    stage('SAST — Semgrep') {
      when { expression { env.REQUIRED_STAGES?.contains('sast') } }
      steps {
        sh 'semgrep --config=auto src/ --json --output=reports/semgrep.json || true'
      }
      post {
        always {
          archiveArtifacts(artifacts: 'reports/semgrep.json', allowEmptyArchive: true)
        }
      }
    }

    stage('Pipeline Drift Check') {
      when { expression { env.REQUIRED_STAGES?.contains('drift-check') } }
      steps {
        script {
          if (env.AI_AVAILABLE == 'true') {
            def result = aiDriftCheck(
              sidecarUrl:     env.AI_SIDECAR_URL,
              jenkinsfile:    readFile('Jenkinsfile'),
              goldenTemplate: 'pipeline{agent any;stages{stage("Build"){steps{sh "build"}}}}',
              serviceName:    env.SERVICE_NAME
            )
            echo "Compliance: ${result.compliance_score}"
          } else {
            echo 'AI unavailable — skipping drift check'
          }
        }
      }
    }

  }

  post {
    failure {
      script {
        if (env.AI_AVAILABLE == 'true') {
          aiFailureAnalysis(
            sidecarUrl:   env.AI_SIDECAR_URL,
            githubToken:  env.GITHUB_TOKEN,
            githubRepo:   env.GITHUB_REPO,
            serviceName:  env.SERVICE_NAME,
            branchName:   env.BUILD_BRANCH,
            buildNumber:  env.BUILD_NUMBER
          )
        }
      }
    }
    always {
      script {
        def result   = currentBuild.result ?: 'UNKNOWN'
        def duration = currentBuild.duration / 1000
        sh """
          echo 'pipeline_duration_seconds{service="${env.SERVICE_NAME}",branch="${env.BUILD_BRANCH}",result="${result}"} ${duration}' \
            | curl -s --data-binary @- ${env.PUSHGATEWAY_URL}/metrics/job/aria-pipeline || true
        """
      }
      cleanWs(cleanWhenFailure: false)
    }
    success {
      echo "✓ ${env.SERVICE_NAME}:${env.BUILD_COMMIT} — pipeline complete"
    }
  }

}
