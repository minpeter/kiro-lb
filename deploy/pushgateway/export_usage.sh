#!/bin/sh
# Fetch the kiro-lb exposition and republish it through Pushgateway.
#
# This mirrors freerouter-probe/export_usage.sh deliberately. kiro-lb's /metrics
# needs a bearer key, and none of the jobs in /opt/monitoring/prometheus.yml
# carry credentials, so the authenticated fetch happens here and Prometheus only
# ever scrapes the unauthenticated Pushgateway.
#
# PUT, not POST: each run must replace the whole grouping. A POST would merge,
# leaving series for a model or account that has since disappeared.
set -eu

: "${KIRO_LB_API_KEY:?KIRO_LB_API_KEY is required}"
metrics_url="${KIRO_LB_METRICS_URL:-http://10.10.10.10:8000/metrics}"
pushgateway_url="${PUSHGATEWAY_URL:-http://10.10.10.3:9091}"
job="${USAGE_JOB:-kiro-lb-usage}"
instance="${USAGE_INSTANCE:-ws}"

metrics="$(curl --fail --silent --show-error \
  --connect-timeout 5 \
  --max-time 15 \
  --header "Authorization: Bearer $KIRO_LB_API_KEY" \
  "$metrics_url")"

printf '%s\n' "$metrics" |
  curl --fail --silent --show-error \
  --connect-timeout 5 \
  --max-time 15 \
  --request PUT \
  --data-binary @- \
  "$pushgateway_url/metrics/job/$job/instance/$instance"
