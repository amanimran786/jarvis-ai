#!/usr/bin/env bash
# scripts/setup.sh — Install ADE dependencies and configure tmux defaults.
set -euo pipefail

echo "==> Checking prerequisites…"

# tmux
if ! command -v tmux &>/dev/null; then
    echo "  Installing tmux…"
    if [[ "$(uname)" == "Darwin" ]]; then
        brew install tmux
    else
        sudo apt-get install -y tmux
    fi
else
    echo "  tmux: $(tmux -V)"
fi

# claude CLI
if ! command -v claude &>/dev/null; then
    echo "  WARNING: 'claude' CLI not found."
    echo "  Install it from: https://claude.ai/code"
    echo "  Then run: claude login"
else
    echo "  claude: $(claude --version 2>/dev/null || echo 'installed')"
fi

# Python deps
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "==> Installing Python packages…"
pip install --quiet psutil 2>/dev/null || true   # optional: CPU stats in ade list

# tmux defaults — write to ~/.tmux.conf only if not already configured
TMUX_CONF="$HOME/.tmux.conf"
ADE_BLOCK="# --- ADE defaults (added by setup.sh) ---"

if ! grep -q "ADE defaults" "$TMUX_CONF" 2>/dev/null; then
    echo "" >> "$TMUX_CONF"
    cat >> "$TMUX_CONF" <<'EOF'
# --- ADE defaults (added by setup.sh) ---
set -g history-limit 50000
set -g mouse on
set -g status-right "#{session_name} | %H:%M"
set -g base-index 1
setw -g pane-base-index 1
EOF
    echo "  Written tmux defaults to $TMUX_CONF"
else
    echo "  tmux defaults already present — skipping."
fi

# Make ade-loop executable
chmod +x "$SCRIPT_DIR/ade-loop"

# Install the `ade` command on PATH via a symlink in ~/bin (or /usr/local/bin)
ADE_BIN="$REPO_ROOT/ade_cmd.py"
if [[ -f "$ADE_BIN" ]]; then
    TARGET_DIR="$HOME/bin"
    mkdir -p "$TARGET_DIR"
    ln -sf "$ADE_BIN" "$TARGET_DIR/ade"
    chmod +x "$ADE_BIN"
    echo "  ade → $TARGET_DIR/ade (add $TARGET_DIR to PATH if needed)"
fi

echo ""
echo "Setup complete. Usage:"
echo "  ade start \"refactor-login\""
echo "  ade start \"fix-header-bug\" --prompt \"The header nav breaks on mobile at <768px\""
echo "  ade list"
echo "  ade watch refactor-login"
echo "  ade sync  refactor-login"
