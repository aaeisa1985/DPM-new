# DPM Platform (v1.1)

A lightweight discretionary portfolio management engine for a single portfolio manager.

## Quickstart

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Start the platform
uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
```

Open your browser at `http://127.0.0.1:8000/ui/dashboard`.
