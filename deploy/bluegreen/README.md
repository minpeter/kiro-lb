# kiro-lb blue/green (homelab POC)

Stable external port **`10.10.10.10:8000`** via `kiro-lb-edge` (HAProxy).
nginx / `kiro.minpeter.internal` **never** change per deploy.

```text
nginx → 10.10.10.10:8000 (edge)
           ├─ kiro-blue  :8001 loopback debug
           └─ kiro-green :8002 loopback debug
```

## Commands

```bash
cd ~/github.com/minpeter/kiro-lb-python

./deploy/bluegreen/deploy.sh --status
./deploy/bluegreen/deploy.sh              # build idle slot → health → flip → stop old
./deploy/bluegreen/deploy.sh --no-build  # same image, flip only
./deploy/bluegreen/deploy.sh --boot      # after reboot: active slot + edge
./deploy/bluegreen/deploy.sh --init      # one-shot legacy single-container → edge+blue
```

Flip mechanism: render `docker/haproxy-edge.generated.cfg` from
`docker/haproxy-edge.cfg.template` for the next slot, then `docker kill -s USR2`
on `kiro-lb-edge` (soft reload). Active slot file: `deploy/bluegreen/active_slot`.
Response header proof: **`X-Kiro-Slot: blue|green`**.

## Rules

- Only the **active** slot should receive traffic (shared `./data` is not dual-writer safe).
- Do not edit proxy nginx for deploys.
- Compose project name must stay **`kiro-lb`** (`-p kiro-lb`).
- Idle slot may be absent; HAProxy uses `init-addr last,libc,none` so missing DNS does not block start.

## Verified (2026-08-03)

- blue→green and green→blue with continuous `/health` probe: **0 non-200**
- L7 `kiro.minpeter.internal/health` + `/v1/models` 200 after flip
