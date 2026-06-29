#!/usr/bin/env python3
import subprocess, time, os

# Kill any zombie on port 7842
subprocess.run("lsof -ti:7842 | xargs kill -9 2>/dev/null", shell=True)
time.sleep(1.5)

# Launch the dashboard
os.chdir(os.path.expanduser("~/jarvis-ai"))
os.execv("/usr/local/bin/python3", ["/usr/local/bin/python3", os.path.expanduser("~/jarvis-ai/jarvis_dashboard.py")])
