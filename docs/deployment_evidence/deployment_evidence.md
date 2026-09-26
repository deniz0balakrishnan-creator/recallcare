# Deployment evidence

**Live URL:** `https://<LIVE_URL>` · **Health:** `https://<LIVE_URL>/healthz` · **Webhook:** `https://<LIVE_URL>/webhook/whatsapp`
**Platform:** exactly one Amazon Lightsail instance (medium: 2 vCPU / 4 GB, Ubuntu 24.04, ap-southeast-1), static IP, systemd service `recallcare`, Caddy with automatic HTTPS (sslip.io hostname).

| File | What it proves | Captured by |
|---|---|---|
| `01_lightsail_console.png` | only one instance, size medium, region Singapore | AWS console |
| `03_healthz.json` | `/healthz` output from the public URL | `curl` |
| `04_server_status.txt` | `systemctl status recallcare caddy` on the instance | ssh |
| `05_whatsapp_roundtrip_log.txt` | server journal lines of a real WhatsApp round-trip (webhook in → guard → reply sent), phone numbers masked | ssh `journalctl` |
| `06_eval_on_server.md` | eval suite executed on the server | ssh |
