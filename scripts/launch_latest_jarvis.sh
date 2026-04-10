#!/usr/bin/env bash

# Simple launcher that relies on macOS for single-instance management
# The 'open -a' command without -n flag will reuse an existing app instance
# or launch a new one if none is running (standard macOS behavior)

exec open -a "$HOME/Applications/Jarvis.app"
