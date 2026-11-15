# Offline unit tests for the Function App harness (no secrets, no network).
#
# Install the runtime and test dependencies once, then run the suite:
#
#   python -m pip install -r harness/requirements.txt -r harness/requirements-dev.txt
#   make test
#
# pytest.ini selects harness/tests, so the command is the same from the repository root
# and in CI (.github/workflows/tests.yml). Pick another interpreter with
# `make test PYTHON=.venv/bin/python`. Without make: `python -m pytest -q`.

PYTHON ?= python

.PHONY: test
test:
	$(PYTHON) -m pytest -q
