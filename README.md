# RecallCare

> **Synthetic data only.** Every patient, phone number and clinic in this repository is fictional. No real patient data is used anywhere.

**RecallCare is a team of AI agents that finds every patient who has fallen behind on dental care, reaches them on WhatsApp in their own language, books them back in, and hands anything clinical or sensitive to a human, with every decision traceable.**

Built by team **Binary Beasts** (K2EZYJRZ) for the NUS-ISS *Show Me Your Agents* hackathon (AWS, Singapore Business Federation).
Problem statement: *Patient Follow-up* for dental clinics.

_This README is a skeleton; sections marked TODO are filled in as each phase lands._

## Architecture
TODO (Phase 2): supervisor + four specialist agents (Triage, Safety guard, Conversation, Scheduling) in LangGraph. Diagram in `docs/architecture.md`.

## Quick start (zero credentials)
```bash
make install
make demo      # mock LLM + in-app WhatsApp simulator on http://127.0.0.1:8000
make test
make eval      # eval suite in mock mode
```

## Configuration
Copy `.env.example` to `.env`. Every variable is documented inline there. TODO: table.

## Evals
TODO (Phase 2): `python -m evals.run --mode mock|live` → `evals/report.md`.

## Deployment
TODO (Phase 4): one Amazon Lightsail medium instance, systemd + Caddy (automatic HTTPS via sslip.io).

## Project docs

## Licence
MIT — see `LICENSE`.
