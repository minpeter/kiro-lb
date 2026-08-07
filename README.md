<p align="center">
  <img src="assets/kiro-lb-banner.png" alt="Kiro LB" width="100%">
</p>

# kiro-lb

OpenAI- and Anthropic-compatible gateway for Kiro (Amazon Q Developer /
CodeWhisperer), with multi-account load balancing and an operations dashboard.

## License

**AGPL-3.0** — see [`LICENSE`](LICENSE) and [`NOTICE.md`](NOTICE.md).

Based on [jwadow/kiro-gateway](https://github.com/jwadow/kiro-gateway)
(Copyright (C) 2025 Jwadow). This tree adds multi-account routing, dashboard,
and related changes (Copyright (C) 2026 minpeter).

## Disclaimer

This project is **not** affiliated with, endorsed by, or sponsored by Amazon
Web Services, Inc. or Anthropic. Use of upstream Kiro / Amazon Q services is
subject to **your** AWS / product terms of service. You are solely responsible
for how you obtain credentials and for compliance with applicable terms and law.

## Quick start

```bash
cp .env.example .env
# set PROXY_API_KEY and DASHBOARD_PASSWORD
docker compose up -d --build
# open http://localhost:8000 — add accounts via dashboard device login
```

See `.env.example` and `AGENTS.md` for configuration details.
Issues: https://github.com/minpeter/kiro-lb-python/issues
