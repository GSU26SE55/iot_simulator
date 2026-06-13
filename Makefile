PY ?= python3
VENV := .venv
ACT := . $(VENV)/bin/activate

.PHONY: help venv install run once test clean lint provision

help:
	@echo "Targets:"
	@echo "  make venv         tạo virtualenv ./.venv"
	@echo "  make install      pip install -r requirements.txt"
	@echo "  make run          chạy simulator + dashboard"
	@echo "  make once         smoke test: gửi 1 batch rồi thoát"
	@echo "  make test         chạy unit test"
	@echo "  make provision    gọi admin endpoint tạo IotDevice (cần ADMIN_TOKEN)"
	@echo "  make clean        xoá venv + logs/queue"

venv:
	$(PY) -m venv $(VENV)

install: venv
	$(ACT) && pip install --upgrade pip && pip install -r requirements.txt

run:
	$(ACT) && python -m src.main

once:
	$(ACT) && python -m src.main --once --no-dashboard

test:
	$(ACT) && python -m unittest discover -s tests -v

provision:
	$(ACT) && python scripts/provision_devices.py --insecure

clean:
	rm -rf $(VENV) logs/queue/*.jsonl
