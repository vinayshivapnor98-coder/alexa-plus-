#!/usr/bin/env bash
# Mac / Linux launcher
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
echo "Open http://127.0.0.1:5000 in Chrome or Edge"
.venv/bin/python web_app.py
