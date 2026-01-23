#!/bin/bash
# HyperAPI SDK Installation Script

set -e

echo "========================================"
echo "  HyperAPI SDK Installer"
echo "========================================"
echo ""

# Check Python version
PYTHON_VERSION=$(python3 --version 2>&1 | cut -d' ' -f2 | cut -d'.' -f1,2)
REQUIRED_VERSION="3.9"

if [ "$(printf '%s\n' "$REQUIRED_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$REQUIRED_VERSION" ]; then
    echo "Error: Python $REQUIRED_VERSION or higher is required (found $PYTHON_VERSION)"
    exit 1
fi

echo "[1/3] Python $PYTHON_VERSION detected"

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SDK_DIR="$(dirname "$SCRIPT_DIR")"

# Install package
echo "[2/3] Installing HyperAPI SDK..."
cd "$SDK_DIR"
pip install -e . --quiet

echo "[3/3] Installation complete!"
echo ""
echo "========================================"
echo "  Next Steps"
echo "========================================"
echo ""
echo "1. Configure your API key:"
echo "   cp scripts/env.sample.sh scripts/env.sh"
echo "   # Edit scripts/env.sh with your API key"
echo "   source scripts/env.sh"
echo ""
echo "2. Test the installation:"
echo "   python -c 'from hyperapi import HyperAPIClient; print(\"OK\")'"
echo ""
echo "3. Run the example:"
echo "   python examples/basic_usage.py"
echo ""
