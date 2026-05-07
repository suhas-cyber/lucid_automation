# lucid_automation
# EV Lab Dashboard (Lucid proxy)
FastAPI service that serves the EV Lab HTML dashboard and aggregates:
- Lucid wall box (charger CGI via `curl`)
- Live updates over WebSocket (`/ws`)
- Optional: Pilot Dingus (USB serial), Waveshare Modbus contactors
- Optional: unified CAN/Modbus capture (`updated_log.py`) and UART logger (`uart_logger_4.py`) via `dashboard_logs.py`
## Requirements
- Python 3.11+ (3.14 OK if all deps install)
- `curl` on PATH (used for HTTPS to the charger)
- Network access to the Lucid charger and any lab SSH hosts you use for logging
## Quick start
```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
