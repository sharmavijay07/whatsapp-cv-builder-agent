.PHONY: demo test

demo:
	./scripts/demo.sh

test:
	.venv/bin/python -m pytest -q
