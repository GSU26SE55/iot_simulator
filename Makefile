PY ?= python3
VENV := .venv
ACT := . $(VENV)/bin/activate
MOCK_PORT ?= 4001

.PHONY: help venv install run demo once test lint mock mock-ota provision clean clean-state \
        anomaly anomaly-list anomaly-check anomaly-verify anomaly-dry

# Backend dùng cho bộ dataset anomaly (mặc định ApiGateway ở cổng 4001).
BACKEND ?= http://localhost:4001

help:
	@echo "Targets:"
	@echo "  make install      tạo .venv + cài phụ thuộc"
	@echo "  make run          chạy simulator + bảng trạng thái (backend thật)"
	@echo "  make demo         chạy simulator trỏ vào backend giả ở cổng $(MOCK_PORT)"
	@echo "  make once         gửi 1 batch rồi thoát (smoke test)"
	@echo "  make test         chạy toàn bộ unit test"
	@echo "  make lint         pyflakes trên src/ tests/ tools/ scripts/"
	@echo "  make mock         backend giả (kiểm hợp đồng nghiêm ngặt) — cổng $(MOCK_PORT)"
	@echo "  make mock-ota     backend giả + offer OTA 1.2.0 (tải + xác minh SHA-256 thật)"
	@echo "  make provision    gọi admin endpoint tạo IotDevice (cần ADMIN_TOKEN)"
	@echo "  make clean-state  xoá state bền vững → thiết bị 'mới bóc hộp'"
	@echo "  make clean        xoá venv + hàng đợi + state"
	@echo ""
	@echo "  ── demo anomaly (BACKEND=$(BACKEND)) ──"
	@echo "  make anomaly-list   liệt kê 20 case + điều kiện của từng case"
	@echo "  make anomaly-check  kiểm tra backend đã đủ điều kiện chưa (KHÔNG gửi gì)"
	@echo "  make anomaly-dry    in payload thật sẽ gửi, KHÔNG gửi"
	@echo "  make anomaly        chạy toàn bộ dataset (~90s: 2 đợt + tách cặp chéo nguồn)"
	@echo "  make anomaly-verify in câu SQL kiểm chứng cảnh báo đã sinh"

venv:
	$(PY) -m venv $(VENV)

install: venv
	$(ACT) && pip install --upgrade pip && pip install -r requirements.txt

run:
	$(ACT) && python -m src.main

demo:
	$(ACT) && IOT_BASE_URL=http://localhost:$(MOCK_PORT) python -m src.main

once:
	$(ACT) && python -m src.main --once --no-dashboard

test:
	$(ACT) && python -m unittest discover -s tests -t . -v

lint:
	$(ACT) && python -m pyflakes src/ tests/ tools/ scripts/

mock:
	$(ACT) && python tools/mock_backend.py --port $(MOCK_PORT)

mock-ota:
	$(ACT) && python tools/mock_backend.py --port $(MOCK_PORT) --offer-version 1.2.0

provision:
	$(ACT) && python scripts/provision_devices.py --insecure

anomaly-list:
	$(ACT) && python -m src.anomaly list

anomaly-check:
	$(ACT) && IOT_BASE_URL=$(BACKEND) python -m src.anomaly check

anomaly-dry:
	$(ACT) && IOT_BASE_URL=$(BACKEND) python -m src.anomaly run --dry-run

anomaly:
	$(ACT) && IOT_BASE_URL=$(BACKEND) python -m src.anomaly run

anomaly-verify:
	$(ACT) && python -m src.anomaly verify

clean-state:
	rm -rf logs/state

clean:
	rm -rf $(VENV) logs/state logs/queue/*.jsonl
