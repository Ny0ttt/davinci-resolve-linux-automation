# make check   -- verify the venv matches requirements.lock, then run the smoke tests
# make test    -- smoke tests only (needs requirements-dev.txt installed)
# make lock    -- after a deliberate package install/upgrade, re-pin requirements.lock
# make ui      -- run the UI in this terminal (http://127.0.0.1:8765)
# make install-service -- start the UI at login (systemd user service); uninstall-service undoes it
PY := venv/bin/python
SHELL := /bin/bash

.PHONY: check deps test lock ui install-service uninstall-service

check: deps test

# Every pin in requirements.lock must be installed at that exact version. Extra packages
# are allowed, because requirements-dev.txt adds pytest on top for `make test`.
deps:
	@missing=$$($(PY) -m pip list --format=freeze \
		| comm -23 <(sort requirements.lock) <(grep -v '^pip==' | sort)); \
	if [ -n "$$missing" ]; then \
		echo "deps: venv does not satisfy requirements.lock -- missing or wrong version:"; \
		echo "$$missing" | sed 's/^/  /'; \
		echo "  fix: $(PY) -m pip install -r requirements.lock   (or 'make lock' if the change was deliberate)"; \
		exit 1; \
	fi; \
	echo "deps: venv satisfies requirements.lock"

test:
	@$(PY) -c "import pytest" 2>/dev/null \
		|| { echo "test: dev deps missing -- $(PY) -m pip install -r requirements-dev.txt"; exit 1; }
	$(PY) -m pytest

# Re-pins the RUNTIME lock. DEV is what requirements-dev.txt adds on top; it must stay out
# of the lock or installing the pipeline would pull a test runner.
DEV := pytest pluggy iniconfig Pygments
lock:
	@$(PY) -m pip list --format=freeze | grep -v '^pip==' \
		| grep -viE "^($(shell echo $(DEV) | tr ' ' '|'))==" > requirements.lock
	@echo "requirements.lock updated ($$(wc -l < requirements.lock) pins; dev deps excluded)"

ui:
	$(PY) src/ui_server.py

UNIT := $(HOME)/.config/systemd/user/video-pipeline-ui.service

install-service:
	mkdir -p $(dir $(UNIT))
	sed 's|@ROOT@|$(CURDIR)|g' ui/video-pipeline-ui.service.in > $(UNIT)
	systemctl --user daemon-reload
	systemctl --user enable --now video-pipeline-ui.service
	@echo "UI running at http://127.0.0.1:8765 (starts at login)"

uninstall-service:
	-systemctl --user disable --now video-pipeline-ui.service
	rm -f $(UNIT)
	systemctl --user daemon-reload
