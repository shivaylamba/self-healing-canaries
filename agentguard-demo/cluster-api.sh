#!/bin/bash
# Terminal 2: the "payments cluster API" (MCP) on the laptop. It prints every call
# that actually reaches it, so a call Agent Guard blocks never shows up here.
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python -u app/sre_tools.py
