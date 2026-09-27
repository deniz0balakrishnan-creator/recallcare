# Deployment evidence

**Live URL:** `https://47-131-92-243.sslip.io` · **Health:** `https://47-131-92-243.sslip.io/healthz` · **Webhook:** `https://47-131-92-243.sslip.io/webhook/whatsapp`
**Platform:** exactly one Amazon Lightsail instance (medium: 2 vCPU / 4 GB, Ubuntu 24.04, ap-southeast-1), static IP, systemd service `recallcare`, Caddy with automatic HTTPS (sslip.io hostname).

| File | What it proves | Captured by |
|---|---|---|
| `01_lightsail_console.png` | only one instance, size medium, region Singapore | AWS console |
| `03_healthz.json` | `/healthz` output from the public URL | `curl` |
| `04_server_status.txt` | `systemctl status recallcare caddy` on the instance | ssh |
| `05_whatsapp_roundtrip_log.txt` | server journal lines of the agents at work on the live server (reminders, guard verdicts, replies). Captured 27 Sep on the **simulator channel** during the demo storyline; replaced by a real WhatsApp round-trip if the Meta test number is connected | ssh `journalctl` |
| `06_eval_on_server.md` | eval suite executed on the server (mock mode, 37/37; the live-model run is in `evals/report_live.md`) | ssh |

**Captured 27 Sep 2026, 15:03 SGT.** Deployed with `make deploy` (rsync + ssh with a deploy key; `.env` copied over SSH, mode 600 on the server, never committed). From the server, `app.doctor --ping` reached the organisers' gateway (Sonnet 4.5, 1.7 s).
