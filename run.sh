#!/bin/bash

# Ensure we're in the right directory
cd "$(dirname "$0")"

# Create virtualenv if not exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtualenv
source venv/bin/activate

# Install requirements if flask isn't found in this venv
if ! python3 -c "import flask" &> /dev/null; then
    echo "Installing requirements..."
    pip install -r requirements.txt
fi

echo "Starting WiFi Amplifier Web GUI..."
python3 app.py
