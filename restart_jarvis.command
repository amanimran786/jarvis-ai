#!/bin/bash
lsof -ti:7842 | xargs kill -9 2>/dev/null
sleep 1
cd ~/jarvis-ai
python3 jarvis_dashboard.py
