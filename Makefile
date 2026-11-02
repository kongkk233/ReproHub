PY ?= .venv/bin/python

.PHONY: venv init pipeline serve test demo status

venv:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

init:
	$(PY) -m reprohub init

pipeline:
	$(PY) -m reprohub pipeline

serve:
	$(PY) -m reprohub serve --scheduler

demo:
	$(PY) -m reprohub demo-seed && $(PY) -m reprohub pipeline --no-collect

status:
	$(PY) -m reprohub status

test:
	$(PY) -m pytest -q -W ignore::DeprecationWarning
