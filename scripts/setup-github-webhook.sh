#!/usr/bin/env bash
# Register GitHub webhook — detects ngrok automatically for localhost Jenkins
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "${SCRIPT_DIR}/../.env" ]] && { set -a; source "${SCRIPT_DIR}/../.env"; set +a; }
: "${GITHUB_TOKEN:?}" ; : "${GITHUB_REPO_OWNER:?}" ; : "${GITHUB_REPO_NAME:?}" ; : "${JENKINS_URL:?}"

J="${JENKINS_URL%/}"
EFFECTIVE="$J"

if echo "$J" | grep -qE "localhost|127\.0\.0\.1"; then
  NGROK=$(curl -sf http://localhost:4040/api/tunnels 2>/dev/null \
    | python3 -c "import sys,json; t=json.load(sys.stdin)['tunnels']; \
      print(next((x['public_url'] for x in t if x['proto']=='https'),''))" 2>/dev/null || echo "")
  if [[ -n "$NGROK" ]]; then
    EFFECTIVE="$NGROK"
    echo "ngrok tunnel detected: $NGROK"
  else
    echo "Jenkins is localhost but no ngrok detected."
    echo "Start ngrok in another terminal: ngrok http 8080"
    read -rp "Or enter public URL manually (Enter to skip): " M
    [[ -n "$M" ]] && EFFECTIVE="${M%/}" || { echo "Skipped — pollSCM fallback active (5 min interval)"; exit 0; }
  fi
fi

WEBHOOK="${EFFECTIVE}/github-webhook/"
echo "Registering webhook: $WEBHOOK"

# Remove existing webhook for same URL
EXISTING=$(curl -sf "https://api.github.com/repos/${GITHUB_REPO_OWNER}/${GITHUB_REPO_NAME}/hooks" \
  -H "Authorization: token ${GITHUB_TOKEN}" \
  | python3 -c "import sys,json; [print(h['id']) for h in json.load(sys.stdin) if '${EFFECTIVE}' in h.get('config',{}).get('url','')]" 2>/dev/null || echo "")
[[ -n "$EXISTING" ]] && curl -sf -X DELETE \
  "https://api.github.com/repos/${GITHUB_REPO_OWNER}/${GITHUB_REPO_NAME}/hooks/${EXISTING}" \
  -H "Authorization: token ${GITHUB_TOKEN}" || true

STATUS=$(curl -sf -o /dev/null -w "%{http_code}" \
  -X POST "https://api.github.com/repos/${GITHUB_REPO_OWNER}/${GITHUB_REPO_NAME}/hooks" \
  -H "Authorization: token ${GITHUB_TOKEN}" -H "Content-Type: application/json" \
  -d "{\"name\":\"web\",\"active\":true,\"events\":[\"push\",\"pull_request\",\"create\"],
       \"config\":{\"url\":\"${WEBHOOK}\",\"content_type\":\"json\",\"insecure_ssl\":\"0\"}}" 2>/dev/null)
[[ "$STATUS" == "201" ]] && echo "✓ Webhook registered: $WEBHOOK" || echo "HTTP $STATUS — check token scopes (need admin:repo_hook)"
