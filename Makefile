# RecallCare — common tasks. Mock mode needs zero credentials.
PY ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: install test seed run demo eval eval-live secret-scan graph clean docs pdf doctor deploy evidence

install:            ## create venv + install pinned deps
	test -d .venv || python3.11 -m venv .venv
	$(PIP) install -q -r requirements.txt

test:               ## unit + integration tests (mock LLM, simulator channel)
	$(PY) -m pytest

seed:               ## (re)generate synthetic data into data/recallcare.db
	$(PY) -m app.seed --reset

run:                ## start the app on http://127.0.0.1:8000
	$(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8000

demo:               ## zero-credential demo: mock LLM + phone simulator (login staff / demo)
	LLM_PROVIDER=mock CHANNEL=simulator $(PY) -m app.demo
	LLM_PROVIDER=mock CHANNEL=simulator DASHBOARD_PASSWORD=$${DASHBOARD_PASSWORD:-demo} $(PY) -m uvicorn app.main:app --host 127.0.0.1 --port 8000

eval:               ## eval suite, mock LLM (free, deterministic)
	$(PY) -m evals.run --mode mock

eval-live:          ## eval suite against the real LLM (costs tokens — run sparingly)
	$(PY) -m evals.run --mode live

graph:              ## regenerate the LangGraph mermaid diagram into docs/architecture.md
	$(PY) -m app.graph --mermaid

secret-scan:        ## scan every tracked file for secrets
	scripts/secret_scan.sh --all

clean:
	rm -rf .pytest_cache data/*.db data/*.db-* evals/runs

doctor:             ## config check (no secrets printed); add ARGS=--ping to test the LLM
	$(PY) -m app.doctor $(ARGS)

docs:               ## regenerate doc fragments from code + latest eval run
	$(PY) scripts/gen_doc_artifacts.py

pdf: docs           ## build docs/pdf/*.pdf (needs pandoc + `pip install typst pypdf`)
	$(PY) scripts/build_pdfs.py

deploy:             ## push to the Lightsail instance (DEPLOY_HOST in .env)
	deploy/deploy.sh

evidence:           ## capture deployment evidence from the live server
	deploy/capture_evidence.sh
