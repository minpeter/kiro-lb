#!/usr/bin/env bash
# kiro-lb blue/green cutover behind kiro-lb-edge (10.10.10.10:8000).
# nginx / kiro.minpeter.internal stay on :8000 — never edited here.
#
# Flip = render HAProxy cfg for the next slot + soft-reload (USR2).
# No Runtime API socket (permission / volume pain); persisted cfg is source of truth.
# Only the active slot may mutate shared runtime state. The idle slot overlaps
# solely for startup health and is stopped immediately after the cutover proof.
#
# Usage:
#   ./deploy/bluegreen/deploy.sh              # build idle slot, flip, stop old
#   ./deploy/bluegreen/deploy.sh --no-build    # flip using existing image
#   ./deploy/bluegreen/deploy.sh --status
#   ./deploy/bluegreen/deploy.sh --init        # one-shot: legacy kiro-lb → edge+blue
#   ./deploy/bluegreen/deploy.sh --boot        # after reboot: start active slot + edge
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

COMPOSE=(docker compose -p kiro-lb -f docker-compose.homelab.yml)
SLOT_FILE="$ROOT/deploy/bluegreen/active_slot"
TEMPLATE="$ROOT/docker/haproxy-edge.cfg.template"
GENERATED="$ROOT/docker/haproxy-edge.generated.cfg"
EDGE=kiro-lb-edge

log() { printf '==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

other_slot() {
  case "$1" in
    blue) echo green ;;
    green) echo blue ;;
    *) die "invalid slot: $1" ;;
  esac
}

slot_port() {
  case "$1" in
    blue) echo 8001 ;;
    green) echo 8002 ;;
    *) die "invalid slot: $1" ;;
  esac
}

slot_service() { echo "kiro-$1"; }

read_active() {
  if [[ -f "$SLOT_FILE" ]]; then
    tr -d '[:space:]' <"$SLOT_FILE"
  else
    echo blue
  fi
}

write_active() {
  printf '%s\n' "$1" >"$SLOT_FILE"
}

set_runtime_writer() {
  local slot=$1
  local attempt
  for attempt in $(seq 1 120); do
    if KIRO_RUNTIME_SLOT="$slot" ROOT="$ROOT" python3 <<'PY'
import os
import sqlite3
import time
from pathlib import Path

path = Path(os.environ["ROOT"]) / "data" / "dashboard.sqlite3"
path.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(path) as conn:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS runtime_writer (id INTEGER PRIMARY KEY CHECK (id = 1), slot TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS credential_refresh_leases "
        "(account_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL)"
    )
    conn.execute("DELETE FROM credential_refresh_leases WHERE expires_at <= ?", (time.time(),))
    if conn.execute("SELECT 1 FROM credential_refresh_leases LIMIT 1").fetchone():
        raise SystemExit(75)
    conn.execute(
        "INSERT INTO runtime_writer(id, slot) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET slot=excluded.slot",
        (os.environ["KIRO_RUNTIME_SLOT"],),
    )
PY
    then
      log "runtime-state writer=$slot"
      return
    fi
    [[ "$attempt" -eq 120 ]] && die "timed out waiting for credential refresh leases before writer handoff"
    sleep 0.25
  done
}

ensure_handoff_secret() {
  if [[ -n "${HANDOFF_SECRET:-}" ]]; then
    export HANDOFF_SECRET
    return
  fi
  HANDOFF_SECRET="$(ROOT="$ROOT" python3 <<'PY'
import os
import secrets
import sqlite3
from pathlib import Path

path = Path(os.environ["ROOT"]) / "data" / "dashboard.sqlite3"
path.parent.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(path) as conn:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS deployment_control "
        "(id INTEGER PRIMARY KEY CHECK (id = 1), handoff_secret TEXT NOT NULL)"
    )
    row = conn.execute("SELECT handoff_secret FROM deployment_control WHERE id = 1").fetchone()
    if row is None:
        value = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO deployment_control(id, handoff_secret) VALUES (1, ?)", (value,))
    else:
        value = row[0]
print(value)
PY
)"
  export HANDOFF_SECRET
}

render_haproxy_cfg() {
  local active=$1
  local blue_state green_state
  case "$active" in
    blue) blue_state=""; green_state="disabled" ;;
    green) blue_state="disabled"; green_state="" ;;
    *) die "render: bad slot $active" ;;
  esac
  [[ -f "$TEMPLATE" ]] || die "missing template $TEMPLATE"
  BLUE_STATE="$blue_state" GREEN_STATE="$green_state" TEMPLATE="$TEMPLATE" GENERATED="$GENERATED" python3 <<'PY'
import os
import re
from pathlib import Path

tpl = Path(os.environ["TEMPLATE"]).read_text()
text = tpl.replace("@@BLUE_STATE@@", os.environ["BLUE_STATE"]).replace(
    "@@GREEN_STATE@@", os.environ["GREEN_STATE"]
)
lines = []
for line in text.splitlines(True):
    if line.lstrip().startswith("server "):
        indent = re.match(r"^[ \t]*", line).group(0)
        body = " ".join(line.split())
        line = f"{indent}{body}\n"
    lines.append(line)
Path(os.environ["GENERATED"]).write_text("".join(lines))
PY
  log "rendered $GENERATED (active=$active)"
}

wait_http() {
  local url=$1 tries=${2:-60}
  local i
  for i in $(seq 1 "$tries"); do
    if curl -fsS -m 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

handoff_post() {
  local port=$1 action=$2
  curl -fsS -m 120 -X POST -H "X-Handoff-Secret: $HANDOFF_SECRET" \
    "http://127.0.0.1:${port}/_internal/handoff/${action}" >/dev/null
}

wait_handoff_ready() {
  local port=$1 tries=${2:-60} i
  for i in $(seq 1 "$tries"); do
    if curl -fsS -m 2 -H "X-Handoff-Secret: $HANDOFF_SECRET" \
      "http://127.0.0.1:${port}/_internal/handoff/ready" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

handoff_status() {
  local port=$1
  curl -sS -m 2 -o /dev/null -w '%{http_code}' -H "X-Handoff-Secret: $HANDOFF_SECRET" \
    "http://127.0.0.1:${port}/_internal/handoff/ready" 2>/dev/null || true
}

edge_slot_header() {
  curl -sS -m 3 -D - -o /dev/null http://10.10.10.10:8000/health 2>/dev/null \
    | awk -F': ' 'tolower($1)=="x-kiro-slot"{gsub(/\r/,"",$2); print $2}'
}

show_status() {
  local active
  active="$(read_active)"
  echo "active_slot=$active"
  echo -n "edge:8000 health="
  curl -sS -m 3 -o /dev/null -w '%{http_code}' http://10.10.10.10:8000/health 2>/dev/null || echo fail
  echo
  echo -n "edge X-Kiro-Slot="
  edge_slot_header || echo "(none)"
  echo
  echo -n "l7 health="
  curl -sS -m 3 -o /dev/null -w '%{http_code}' http://kiro.minpeter.internal/health 2>/dev/null || echo fail
  echo
  docker ps -a --filter name='kiro-lb' --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
}

ensure_edge() {
  if ! docker ps --format '{{.Names}}' | grep -qx "$EDGE"; then
    log "start edge"
    "${COMPOSE[@]}" up -d --no-build edge
  fi
}

soft_reload_edge() {
  ensure_edge
  # Validate cfg inside container before reload
  docker exec "$EDGE" haproxy -c -f /usr/local/etc/haproxy/haproxy.cfg >/dev/null
  log "edge soft-reload (USR2)"
  docker kill -s USR2 "$EDGE" >/dev/null
  sleep 1
}

prove_slot() {
  local want=$1 tries=${2:-40} got=""
  local i
  for i in $(seq 1 "$tries"); do
    if curl -fsS -m 2 http://10.10.10.10:8000/health >/dev/null 2>&1; then
      got="$(edge_slot_header || true)"
      [[ "$got" == "$want" ]] && return 0
    fi
    sleep 0.25
  done
  die "proof failed: X-Kiro-Slot='$got' want '$want'"
}

cmd_init() {
  ensure_handoff_secret
  log "init: render cfg active=blue"
  write_active blue
  set_runtime_writer blue
  render_haproxy_cfg blue

  log "init: build image"
  "${COMPOSE[@]}" build kiro-blue

  if docker ps -a --format '{{.Names}}' | grep -qx 'kiro-lb'; then
    log "init: stopping legacy container kiro-lb (brief blip once)"
    docker stop kiro-lb >/dev/null
    docker rm kiro-lb >/dev/null
  fi

  docker rm -f kiro-lb-green >/dev/null 2>&1 || true
  # drop old volume if any from earlier socket attempt
  docker volume rm kiro-lb_kiro-lb-haproxy-run >/dev/null 2>&1 || true

  log "init: start kiro-blue + edge"
  "${COMPOSE[@]}" up -d --no-build --force-recreate kiro-blue edge

  log "init: wait edge + L7"
  wait_http http://127.0.0.1:8001/health 60 || die "blue health failed"
  wait_http http://10.10.10.10:8000/health 60 || die "edge health failed"
  wait_http http://kiro.minpeter.internal/health 30 || die "L7 health failed"
  prove_slot blue
  show_status
  log "init OK — next: ./deploy/bluegreen/deploy.sh"
}

cmd_boot() {
  ensure_handoff_secret
  local active svc
  active="$(read_active)"
  svc="$(slot_service "$active")"
  log "boot: active=$active"
  set_runtime_writer "$active"
  render_haproxy_cfg "$active"
  "${COMPOSE[@]}" up -d --no-build "$svc" edge
  wait_http http://10.10.10.10:8000/health 60 || die "boot edge health failed"
  prove_slot "$active"
  show_status
}

cmd_deploy() {
  ensure_handoff_secret
  local no_build=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --no-build) no_build=1; shift ;;
      *) die "unknown arg: $1" ;;
    esac
  done

  local current next cur_svc next_svc current_port next_port
  current="$(read_active)"
  next="$(other_slot "$current")"
  cur_svc="$(slot_service "$current")"
  next_svc="$(slot_service "$next")"
  current_port="$(slot_port "$current")"
  next_port="$(slot_port "$next")"

  local deploy_succeeded=0 legacy_point_of_no_return=0
  rollback_deploy() {
    local status=$?
    [[ "$deploy_succeeded" -eq 1 ]] && return "$status"
    trap - EXIT
    if [[ "$legacy_point_of_no_return" -eq 1 ]]; then
      log "legacy migration passed its credential-rotation point of no return; leaving the old image stopped"
      log "fix the new slot forward, then rerun this deploy command"
      return "$status"
    fi
    log "deploy failed: restoring writer, HAProxy, and active slot to $current"
    set +e
    "${COMPOSE[@]}" up -d --no-build "$cur_svc" >/dev/null
    set_runtime_writer "$current"
    handoff_post "$current_port" activate >/dev/null 2>&1 || true
    render_haproxy_cfg "$current"
    soft_reload_edge
    write_active "$current"
    set -e
    return "$status"
  }
  trap rollback_deploy EXIT

  [[ -f "$GENERATED" ]] || render_haproxy_cfg "$current"

  log "deploy: active=$current → next=$next"
  set_runtime_writer "$current"

  if [[ "$no_build" -eq 0 ]]; then
    log "build $next_svc (traffic stays on $current)"
    "${COMPOSE[@]}" build "$next_svc"
  fi

  # The release that introduced SQLite handoff cannot quiesce an older active
  # image. For that one transition, stop it gracefully before the new image
  # imports its final credentials.json/state.json snapshot. Later releases use
  # the zero-downtime handoff path below.
  local current_handoff_status
  current_handoff_status="$(handoff_status "$current_port")"
  case "$current_handoff_status" in
    200) ;;
    404)
      log "legacy active slot detected; performing one-time graceful migration cutover"
      "${COMPOSE[@]}" stop "$cur_svc" >/dev/null
      # Once the new image can refresh credentials, rolling back to an image
      # that cannot read SQLite overlays could invalidate authentication.
      legacy_point_of_no_return=1
      set_runtime_writer "$next"
      "${COMPOSE[@]}" up -d --no-build --force-recreate "$next_svc"
      wait_http "http://127.0.0.1:${next_port}/health" 90 || die "$next slot health failed"
      wait_handoff_ready "$next_port" 30 || die "new slot readiness failed"
      render_haproxy_cfg "$next"
      soft_reload_edge
      prove_slot "$next"
      write_active "$next"
      wait_http http://kiro.minpeter.internal/health 15 || die "post-migration L7 failed"
      show_status
      deploy_succeeded=1
      trap - EXIT
      log "deploy OK (legacy migration cutover)"
      return
      ;;
    403) die "active slot rejected the handoff secret; refusing to treat it as legacy" ;;
    503) die "active slot has handoff support but is not active; inspect slot ownership before deploying" ;;
    *) die "could not verify active-slot handoff support (HTTP ${current_handoff_status:-000})" ;;
  esac

  log "start $next_svc"
  "${COMPOSE[@]}" up -d --no-build --force-recreate "$next_svc"

  log "wait direct slot :$next_port"
  wait_http "http://127.0.0.1:${next_port}/health" 90 || die "$next slot health failed"

  ensure_edge
  wait_http http://10.10.10.10:8000/health 30 || die "edge unhealthy before flip"

  # Drain the old writer before changing ownership. The standby started
  # quiesced, then reloads this final snapshot before it can receive traffic.
  log "quiesce $current and persist final runtime state"
  handoff_post "$current_port" quiesce || die "old slot handoff drain failed"
  log "handoff writer=$next; reload standby"
  set_runtime_writer "$next"
  handoff_post "$next_port" activate || die "new slot activation failed"
  wait_handoff_ready "$next_port" 30 || die "new slot handoff readiness failed"
  render_haproxy_cfg "$next"
  soft_reload_edge
  prove_slot "$next"
  write_active "$next"

  log "stop old $cur_svc"
  "${COMPOSE[@]}" stop "$cur_svc" >/dev/null || true

  wait_http http://10.10.10.10:8000/health 15 || die "post-stop edge health failed"
  wait_http http://kiro.minpeter.internal/health 15 || die "post-stop L7 failed"
  prove_slot "$next"
  show_status
  deploy_succeeded=1
  trap - EXIT
  log "deploy OK"
}

main() {
  case "${1:-deploy}" in
    --status|status) show_status ;;
    --init|init) cmd_init ;;
    --boot|boot) cmd_boot ;;
    --no-build)
      shift || true
      cmd_deploy --no-build "$@"
      ;;
    deploy|--deploy)
      shift || true
      cmd_deploy "$@"
      ;;
    -h|--help|help)
      sed -n '2,16p' "$0"
      ;;
    *)
      cmd_deploy "$@"
      ;;
  esac
}

main "$@"
