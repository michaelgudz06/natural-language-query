#!/bin/bash
# Double-click this to launch the Fundable planner tester.
cd "$(dirname "$0")"
echo "Starting the Fundable planner tester…"
echo "A browser tab will open at http://localhost:8000"
echo "Leave this window open while testing. Close it (or Ctrl-C) to stop."
python3 serve.py
