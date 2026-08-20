# Developer entrypoints. `make check` is what CI runs.
PYTHON ?= python3

.PHONY: help test lint package package-check dist check clean

help:
	@echo "test          run the unit test suite"
	@echo "lint          run ruff over the source tree"
	@echo "package       regenerate neuroicu_tts_addon/ from addon/"
	@echo "package-check fail if the generated package is stale"
	@echo "dist          build packages/neuroicu_tts_addon.ankiaddon"
	@echo "check         test + package-check + lint"

test:
	PYTHONPATH=addon $(PYTHON) -m unittest discover -s addon -v

lint:
	ruff check .

package:
	$(PYTHON) tools/package.py

package-check:
	$(PYTHON) tools/package.py --check

dist: package
	$(PYTHON) tools/package.py --zip

check: test package-check lint

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	find . -name '*.pyc' -delete
