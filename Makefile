.PHONY: help install install-user install-pipx install-simple dev test uninstall config print-config clean

DIST_NAME := ai-log-analyzer
CONFIG_EXAMPLE := ai-log-analyzer.conf.example
PYTHON ?= python3
PIPX ?= pipx
VENV ?= .venv
VENV_PYTHON := $(VENV)/bin/python
# Tests run inside the local .venv when it exists, otherwise with the system interpreter.
TEST_PYTHON := $(shell test -x $(VENV_PYTHON) && echo $(VENV_PYTHON) || echo $(PYTHON))

help:
	@echo "Targets:"
	@echo "  make install        Install standalone script/config interactively (recommended: run with sudo)"
	@echo "  make install-simple Install standalone script/config interactively"
	@echo "  make install-pipx   Install CLI tool as Python package with pipx and configure it"
	@echo "  make install-user   Alias for make install-simple"
	@echo "  make dev            Create/update local .venv and install editable (with test dependencies)"
	@echo "  make test           Run syntax checks and the pytest suite (uses .venv if present)"
	@echo "  make config         Alias for make install-simple"
	@echo "  make print-config   Print default user config path"
	@echo "  make uninstall      Uninstall from pipx, then try pip as fallback"
	@echo "  make clean          Remove build artifacts and local virtualenv"

install:
	@if [ "$$(id -u)" -ne 0 ]; then \
		echo ""; \
		echo "WARNING: You are running make install as a non-root user."; \
		echo "System-wide installation is recommended with:"; \
		echo "  sudo make install"; \
		printf "Continue with installation as non-root user? Type 'yes': "; \
		read answer; \
		if [ "$$answer" != "yes" ]; then \
			echo "Installation aborted."; \
			exit 1; \
		fi; \
	fi
	$(MAKE) install-simple

install-user: install-simple

install-simple:
	sh scripts/install-simple.sh

install-pipx:
	$(PIPX) install .
	sh scripts/install-simple.sh --config-only

dev:
	$(PYTHON) -m venv $(VENV)
	$(VENV_PYTHON) -m pip install --upgrade pip
	$(VENV_PYTHON) -m pip install -e ".[dev]"
	@echo ""
	@echo "Development environment ready."
	@echo "Run: $(VENV)/bin/ai-log-analyzer --help"

test:
	$(TEST_PYTHON) -m py_compile src/ai_log_analyzer
	$(TEST_PYTHON) src/ai_log_analyzer --version
	bash -n scripts/install-simple.sh
	$(TEST_PYTHON) -m pytest -q tests

config: install-simple

print-config:
	$(PYTHON) src/ai_log_analyzer --print-config-path

uninstall:
	-$(PIPX) uninstall $(DIST_NAME)
	-$(PYTHON) -m pip uninstall -y $(DIST_NAME)

clean:
	rm -rf build dist *.egg-info src/*.egg-info .pytest_cache .mypy_cache .ruff_cache $(VENV)
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
