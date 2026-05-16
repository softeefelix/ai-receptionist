#!/bin/bash
# Reads credentials from .env files and sets them as GitHub Actions secrets.
set -e

REPO="softeefelix/ai-receptionist"
PROJECT_ENV="$(dirname "$0")/.env"
DASHBOARD_ENV="$HOME/softeedashboard/.env"
TOKENS_FILE="$(dirname "$0")/jobber_tokens.json"

load_env() {
  local file="$1"
  if [ -f "$file" ]; then
    while IFS='=' read -r key val; do
      [[ "$key" =~ ^#.*$ || -z "$key" ]] && continue
      eval "export ENV_${key}=\"${val}\""
    done < <(grep -v '^#' "$file" | grep '=')
  fi
}

load_env "$DASHBOARD_ENV"
load_env "$PROJECT_ENV"  # project overrides dashboard

echo "Setting GitHub secrets for $REPO..."

printf '%s' "$ENV_RETELL_API_KEY"       | gh secret set RETELL_API_KEY       --repo "$REPO"
printf '%s' "$ENV_AGENTMAIL_API_KEY"    | gh secret set AGENTMAIL_API_KEY    --repo "$REPO"
printf '%s' "$ENV_AGENTMAIL_INBOX"      | gh secret set AGENTMAIL_INBOX      --repo "$REPO"
printf '%s' "$ENV_NOTIFY_EMAIL"         | gh secret set NOTIFY_EMAIL         --repo "$REPO"
printf '%s' "$ENV_JOBBER_CLIENT_ID"     | gh secret set JOBBER_CLIENT_ID     --repo "$REPO"
printf '%s' "$ENV_JOBBER_CLIENT_SECRET" | gh secret set JOBBER_CLIENT_SECRET --repo "$REPO"
printf '%s' "$ENV_SLACK_WEBHOOK_URL"    | gh secret set SLACK_WEBHOOK_URL    --repo "$REPO"
printf '%s' "$ENV_DB_URL"               | gh secret set DB_URL               --repo "$REPO"

if [ -f "$TOKENS_FILE" ]; then
  cat "$TOKENS_FILE" | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)))" \
    | gh secret set JOBBER_TOKENS_JSON --repo "$REPO"
fi

echo "All secrets set successfully."
