PY ?= .venv/bin/python
RUFF ?= .venv/bin/ruff

.PHONY: help lint test seed seed-dry seed-reset seed-clear demo

help:
	@echo "make lint        — ruff over src/, scripts/, tests/"
	@echo "make test        — offline pytest suite (no keys, no Neo4j)"
	@echo "make seed-dry    — validate seed/graph.json and print the write plan (no connection)"
	@echo "make seed        — idempotent upsert of the seed corpus into Neo4j"
	@echo "make seed-reset  — drop the seed's three sources, then re-seed"
	@echo "make seed-clear  — drop the seed's three sources and exit"
	@echo "make demo        — seed-reset, then run the server on :8000"

lint:
	$(RUFF) check src/ scripts/ tests/

test:
	$(PY) -m pytest -q

seed-dry:
	$(PY) -m scripts.seed_graph --dry-run

seed:
	$(PY) -m scripts.seed_graph

seed-reset:
	$(PY) -m scripts.seed_graph --reset

seed-clear:
	$(PY) -m scripts.seed_graph --clear

demo: seed-reset
	$(PY) -m src.main
