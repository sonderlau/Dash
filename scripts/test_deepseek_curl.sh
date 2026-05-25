#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${ROOT_DIR}/.env.local"

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

: "${OPENAI_API_KEY:?OPENAI_API_KEY is required}"

OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://api.deepseek.com}"
MODEL_NAME="${MODEL_NAME:-deepseek-v4-flash}"
ENDPOINT="${OPENAI_BASE_URL%/}/chat/completions"

CONNECT_TIMEOUT="${CONNECT_TIMEOUT:-20}"
MAX_TIME="${MAX_TIME:-300}"
OUT_DIR="${ROOT_DIR}/tmp"
BODY_FILE="${OUT_DIR}/deepseek-response-body.json"
HEADER_FILE="${OUT_DIR}/deepseek-response-headers.txt"

mkdir -p "${OUT_DIR}"

read -r -d '' REQUEST_BODY <<JSON || true
{
  "model": "${MODEL_NAME}",
  "temperature": 0.1,
  "max_tokens": 64,
  "response_format": {"type": "json_object"},
  "messages": [
    {
      "role": "system",
      "content": "Reply with a compact JSON object only."
    },
    {
      "role": "user",
      "content": "Return {\"ok\":true,\"provider\":\"deepseek\"}"
    }
  ]
}
JSON

echo "Testing DeepSeek endpoint..."
echo "endpoint: ${ENDPOINT}"
echo "model: ${MODEL_NAME}"
echo "connect-timeout: ${CONNECT_TIMEOUT}s"
echo "max-time: ${MAX_TIME}s"
echo

curl \
  --silent \
  --show-error \
  --location \
  --http1.1 \
  --connect-timeout "${CONNECT_TIMEOUT}" \
  --max-time "${MAX_TIME}" \
  --output "${BODY_FILE}" \
  --dump-header "${HEADER_FILE}" \
  --write-out $'http_code=%{http_code}\nremote_ip=%{remote_ip}\nssl_verify=%{ssl_verify_result}\ntime_namelookup=%{time_namelookup}\ntime_connect=%{time_connect}\ntime_appconnect=%{time_appconnect}\ntime_pretransfer=%{time_pretransfer}\ntime_starttransfer=%{time_starttransfer}\ntime_total=%{time_total}\nsize_download=%{size_download}\n' \
  --header "Authorization: Bearer ${OPENAI_API_KEY}" \
  --header "Content-Type: application/json" \
  --data "${REQUEST_BODY}" \
  "${ENDPOINT}"

echo
echo "Saved headers to ${HEADER_FILE}"
echo "Saved body to ${BODY_FILE}"
echo
echo "Response preview:"
sed -n '1,40p' "${BODY_FILE}"
