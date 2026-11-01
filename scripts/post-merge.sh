#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# Reproduce the checked-in Python dependency environment without prompts.
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-.pythonlibs}"
uv sync --frozen --no-progress

# Fail the merge setup if the bot or its local data layer is broken.
python -m unittest discover -s tests -v
