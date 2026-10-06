#!/usr/bin/env bash
# End-to-end smoke test against a running `docker compose up` stack:
# register, upload a text PDF and a scanned PDF, wait for the worker, check the extracted text.
set -euo pipefail

API=${API:-http://localhost:8000}
WORK=$(mktemp -d)
cd "$(dirname "$0")/.."

echo "Waiting for the API..."
for _ in $(seq 1 60); do curl -fs "$API/health/ready" >/dev/null && break; sleep 2; done
curl -fs "$API/health/ready"
echo

EMAIL="smoke-$RANDOM@example.com"
TOKEN=$(curl -fs -X POST "$API/v1/auth/register" -H "content-type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"correct-horse-battery\",\"organization_name\":\"Smoke\"}" \
  | jq -r .access_token)
AUTH="Authorization: Bearer $TOKEN"

docker compose exec -T api python - < scripts/make_smoke_docs.py
docker compose cp api:/tmp/text.pdf "$WORK/text.pdf"
docker compose cp api:/tmp/scan.pdf "$WORK/scan.pdf"

check() {
  local file=$1 expect_source=$2 expect_text=$3 doc status=""
  doc=$(curl -fs -X POST "$API/v1/documents" -H "$AUTH" -F "file=@$WORK/$file" | jq -r .document_id)
  echo "Uploaded $file as $doc"
  for _ in $(seq 1 60); do
    status=$(curl -fs "$API/v1/documents/$doc/status" -H "$AUTH" | jq -r .status)
    if [ "$status" = "PROCESSED" ] || [ "$status" = "FAILED" ]; then break; fi
    sleep 2
  done
  curl -fs "$API/v1/documents/$doc/status" -H "$AUTH" \
    | jq -c '{status, version: (.current_version | {page_count, ocr_page_count, language, error_code})}'
  if [ "$status" != "PROCESSED" ]; then echo "FAILED: $file ended as $status"; exit 1; fi
  curl -fs "$API/v1/documents/$doc/pages" -H "$AUTH" \
    | jq -e --arg s "$expect_source" --arg t "$expect_text" \
        '.items[0].source == $s and (.items[0].text | ascii_downcase | contains($t))' >/dev/null \
    || { echo "FAILED: unexpected page content for $file"; exit 1; }
  echo "OK: $file"
}

check text.pdf TEXT "revenue increased"
check scan.pdf OCR "invoice"
echo "Smoke test passed"
