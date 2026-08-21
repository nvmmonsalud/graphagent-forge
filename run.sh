#!/bin/bash
# ============================================================
# GraphAgent Forge — Start Script
# ============================================================
set -euo pipefail

cd "$(dirname "$0")"

echo "🧠 GraphAgent Forge — Starting up..."
echo ""

# Check .env
if [ ! -f .env ]; then
    echo "⚠️  No .env file found. Copying from .env.example..."
    cp .env.example .env
    echo "   Edit .env with your API keys, then re-run this script."
    exit 1
fi

# Check venv
if [ ! -d .venv ]; then
    echo "📦 Creating virtual environment..."
    /opt/homebrew/bin/python3.13 -m venv .venv
fi

source .venv/bin/activate

# Install deps
echo "📦 Installing dependencies..."
pip install -q -r requirements.txt

echo ""
echo "🚀 Starting GraphAgent Forge on http://localhost:8000"
echo "   Press Ctrl+C to stop"
echo ""

python -m src.main
