#!/usr/bin/env bash
# Modulo API Example: Pipeline CRUD - curl
#
# Demonstrates pipeline list, create, get, update, and delete with curl.
#
# Usage:
#   export FARNA_CONTROL_URL=http://localhost:8000
#   export FARNA_CONTROL_EMAIL=admin@example.com
#   export FARNA_CONTROL_PASSWORD=changeme
#   bash pipelines/curl.sh

set -euo pipefail

BASE_URL="${FARNA_CONTROL_URL:-http://localhost:8000}"
EMAIL="${FARNA_CONTROL_EMAIL:?FARNA_CONTROL_EMAIL is required}"
PASSWORD="${FARNA_CONTROL_PASSWORD:?FARNA_CONTROL_PASSWORD is required}"

echo "=== Login ==="
TOKEN=$(curl -s -X POST "$BASE_URL/api/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg e "$EMAIL" --arg p "$PASSWORD" '{email: $e, password: $p}')" \
  | jq -r '.access_token')

AUTH="Authorization: Bearer $TOKEN"

echo ""
echo "=== List Pipelines ==="
curl -s "$BASE_URL/api/v1/pipelines?page=1&page_size=20" -H "$AUTH" | jq

echo ""
echo "=== Create Pipeline ==="
PIPELINE_ID=$(curl -s -X POST "$BASE_URL/api/v1/pipelines" \
  -H "Content-Type: application/json" \
  -H "$AUTH" \
  -d '{
    "name": "PR Review Pipeline",
    "description": "Automated PR review for code quality",
    "visibility": "org",
    "max_concurrent_runs": 3
  }' | tee /dev/stderr | jq -r '.id')
echo "Created pipeline: $PIPELINE_ID"

echo ""
echo "=== Get Pipeline Detail ==="
curl -s "$BASE_URL/api/v1/pipelines/$PIPELINE_ID" -H "$AUTH" | jq

echo ""
echo "=== Update Pipeline ==="
curl -s -X PATCH "$BASE_URL/api/v1/pipelines/$PIPELINE_ID" \
  -H "Content-Type: application/json" \
  -H "$AUTH" \
  -d '{
    "description": "Updated: now handles security scanning",
    "max_concurrent_runs": 5
  }' | jq

echo ""
echo "=== Delete Pipeline ==="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X DELETE \
  "$BASE_URL/api/v1/pipelines/$PIPELINE_ID" -H "$AUTH"

echo ""
echo "Done."
