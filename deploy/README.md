# Observability deployment assets

Wiring for `GET /metrics` (`kiro/metrics.py`). These files describe this
operator's homelab and carry its private addresses; treat them as a worked
example rather than a drop-in.

## Why the metrics are pushed, not scraped

`/metrics` requires a `/v1` bearer key, and no job in the homelab's
`prometheus.yml` carries credentials. Rather than make authenticated pull the
first such job, a workstation timer does the authenticated fetch and republishes
to Pushgateway, which Prometheus already scrapes with `honor_labels: true`:

```
kiro-lb GET /metrics          (Authorization: Bearer <key>)
   |  every 30s, systemd timer
   v
PUT http://pushgateway:9091/metrics/job/kiro-lb-usage/instance/ws
   |
   v
Prometheus job_name=pushgateway, honor_labels=true
   -> series carry job="kiro-lb-usage", instance="ws"
```

`job` and `instance` are therefore absent from the exposition itself:
Pushgateway owns them. `PUT` (not `POST`) replaces the whole grouping each run,
so a model or account that disappears does not leave a stale series behind.

The same layout backs `freerouter-usage-export.*`; keeping both identical means
one convention to reason about.

## Files

| Path | Goes to |
|---|---|
| `pushgateway/export_usage.sh` | anywhere the timer can reach both hosts |
| `pushgateway/kiro-lb-usage-export.service` | `~/.config/systemd/user/` (symlink) |
| `pushgateway/kiro-lb-usage-export.timer` | `~/.config/systemd/user/` (symlink) |
| `pushgateway/kiro-lb.env.example` | copy to `kiro-lb.env`, `chmod 600` |
| `grafana/kiro-lb.json` | `/opt/monitoring/grafana/dashboards/kiro-lb/` |

## Installing the exporter timer

```bash
cp deploy/pushgateway/* ~/homelab/kiro-lb-probe/
cd ~/homelab/kiro-lb-probe
cp kiro-lb.env.example kiro-lb.env && chmod 600 kiro-lb.env   # then edit
systemctl --user link  ~/homelab/kiro-lb-probe/kiro-lb-usage-export.service
systemctl --user link  ~/homelab/kiro-lb-probe/kiro-lb-usage-export.timer
systemctl --user daemon-reload
systemctl --user enable --now kiro-lb-usage-export.timer
```

`EnvironmentFile` in the unit points at an absolute path, so adjust it if the
directory differs.

## Installing the dashboard

The Grafana container mounts `/opt/monitoring/grafana/dashboards` and its
provisioning directory, so the dashboard is a file, not an API import. Add a
provider once:

```yaml
# /opt/monitoring/grafana/provisioning/dashboards/dashboards.yml
  - name: kiro-lb
    orgId: 1
    folder: kiro-lb
    type: file
    disableDeletion: false
    editable: true
    options:
      path: /var/lib/grafana/dashboards/kiro-lb
```

Then place the JSON and reload. The reload API avoids restarting Grafana, which
matters because the same instance serves unrelated dashboards:

```bash
scp deploy/grafana/kiro-lb.json \
    apps:/opt/monitoring/grafana/dashboards/kiro-lb/kiro-lb.json
curl -X POST -u admin:admin \
    http://10.10.10.3:3000/api/admin/provisioning/dashboards/reload
```

Lands at `/d/kiro-lb/kiro-lb-gateway`.

## Two things the dashboard encodes

**Rate windows are 5m, not `$__rate_interval`.** Pushgateway republishes every
30s, so a shorter window can fall entirely inside one push and read as zero.

**Latency is a mean, not a quantile.** `kiro_lb_request_latency_seconds` is a
summary without quantiles, so `_sum / _count` is all there is. There is
deliberately no p95 panel implying otherwise.

**Latency is not throughput.** That summary is recorded by the request-log
middleware, which stops when the handler returns — first-byte time for a stream.
Throughput has its own pair, measured inside the streaming generators:

```promql
sum by (model) (rate(kiro_lb_timed_output_tokens_total[5m]))
  / clamp_min(sum by (model) (rate(kiro_lb_generation_seconds_total[5m])), 0.001)
```

The numerator is deliberately not `kiro_lb_tokens_total`: that counts every
request, including ones with no timing, and dividing it by a partial duration
reported 82,752 tok/s on the live store. `kiro_lb_generation_seconds_total` on its
own is also useful — as a rate it reads as concurrent generations, since 2 means
two upstream responses were being produced at once.

Token counts use Grafana's native `short` unit (`1.06 Bil`, `3.88 Mil`). Request
counts are left unscaled: at four or five digits, `23.1 K` loses more than it
saves.

## Coupling to the exporter

`tests/unit/test_dashboard_provisioning.py` asserts every metric the dashboard
queries is one the exporter actually emits. Renaming a metric without updating
the JSON fails the suite rather than silently emptying a panel — which is the
whole reason these files live in the repo instead of only on the monitoring host.

The `datasource.uid` (`PBFA97CFB590B2093`) is this Grafana's Prometheus
datasource. On another install, either match the uid or rewrite it after import.
