# Public demo and the cold-start check.
# `make demo` does not need Hermes. `make check` is the pre-submit run.

PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)
DEMO_PORT ?= 8788
CHECK_PORT ?= 18791

.PHONY: demo check

demo:
	GAUGE_PORT=$(DEMO_PORT) GAUGE_FIXTURE=1 $(PYTHON) webapp-demo/server.py --open --verify

check:
	$(PYTHON) -m pytest hermes-gauge/tests webapp-demo/test_server.py
	GAUGE_PORT=$(CHECK_PORT) GAUGE_FIXTURE=1 GAUGE_QUIET=1 $(PYTHON) webapp-demo/server.py --self-check
