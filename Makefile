# GNU make. On Windows without make: python scripts/tasks.py <target>
ifeq ($(OS),Windows_NT)
	PY := python
else
	PY := python3
endif

.PHONY: setup dev test eval seed score-once

setup:
	$(PY) scripts/tasks.py setup

dev:
	$(PY) scripts/tasks.py dev

test:
	$(PY) scripts/tasks.py test

eval:
	$(PY) scripts/tasks.py eval

seed:
	$(PY) scripts/tasks.py seed

score-once:
	$(PY) scripts/tasks.py score-once
