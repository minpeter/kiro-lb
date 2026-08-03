#!/usr/bin/env bash
# kiro-lb blue/green cutover behind kiro-lb-edge (10.10.10.10:8000).
# nginx / kiro.minpeter.internal stay on :8000 — never edited here.
#
# Flip = render HAProxy cfg for the next slot + soft-reload (USR2).
# No Runtime API socket (permission / volume pain); persisted cfg is source of truth.
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
  log "init: render cfg active=blue"
  write_active blue
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
  local active svc
  active="$(read_active)"
  svc="$(slot_service "$active")"
  log "boot: active=$active"
  render_haproxy_cfg "$active"
  "${COMPOSE[@]}" up -d --no-build "$svc" edge
  wait_http http://10.10.10.10:8000/health 60 || die "boot edge health failed"
  prove_slot "$active"
  show_status
}

cmd_deploy() {
  local no_build=0
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --no-build) no_build=1; shift ;;
      *) die "unknown arg: $1" ;;
    esac
  done

  local current next cur_svc next_svc next_port
  current="$(read_active)"
  next="$(other_slot "$current")"
  cur_svc="$(slot_service "$current")"
  next_svc="$(slot_service "$next")"
  next_port="$(slot_port "$next")"

  [[ -f "$GENERATED" ]] || render_haproxy_cfg "$current"

  log "deploy: active=$current → next=$next"

  if [[ "$no_build" -eq 0 ]]; then
    log "build $next_svc (traffic stays on $current)"
    "${COMPOSE[@]}" build "$next_svc"
  fi

  log "start $next_svc"
  "${COMPOSE[@]}" up -d --no-build --force-recreate "$next_svc"

  log "wait direct slot :$next_port"
  wait_http "http://127.0.0.1:${next_port}/health" 90 || die "$next slot health failed"

  ensure_edge
  wait_http http://10.10.10.10:8000/health 30 || die "edge unhealthy before flip"

  log "flip: render active=$next + soft-reload"
  write_active "$next"
  render_haproxy_cfg "$next"
  soft_reload_edge
  prove_slot "$next"

  log "stop old $cur_svc"
  "${COMPOSE[@]}" stop "$cur_svc" >/dev/null || true

  wait_http http://10.10.10.10:8000/health 15 || die "post-stop edge health failed"
  wait_http http://kiro.minpeter.internal/health 15 || die "post-stop L7 failed"
  prove_slot "$next"
  show_status
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
