#!/bin/bash
# Telegram-Claude Bridge — one-shot setup script for macOS
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
USERNAME="$(whoami)"
PLIST_NAME="com.${USERNAME}.telegram-claude-bridge"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

echo "=== Telegram-Claude Bridge Setup ==="
echo "Repo: $REPO_DIR"
echo "User: $USERNAME"
echo ""

# 1. Python venv
if [ ! -d "$REPO_DIR/venv" ]; then
    echo "[1/5] Creating Python venv..."
    python3 -m venv "$REPO_DIR/venv"
else
    echo "[1/5] venv already exists, skipping."
fi

# 2. Install dependencies
echo "[2/5] Installing dependencies..."
"$REPO_DIR/venv/bin/pip" install -q -r "$REPO_DIR/requirements.txt"

# 3. .env
if [ ! -f "$REPO_DIR/.env" ]; then
    echo "[3/5] Creating .env from example..."
    cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
    echo "      ⚠️  Edit $REPO_DIR/.env and fill in your tokens before starting."
else
    echo "[3/5] .env already exists, skipping."
fi

# 4. Detect claude path
CLAUDE_PATH="$(which claude 2>/dev/null || echo '')"
if [ -n "$CLAUDE_PATH" ]; then
    echo "[4/5] Found claude at: $CLAUDE_PATH"
    # Update CLAUDE_PATH in .env if it's still the placeholder
    if grep -q "CLAUDE_PATH=/usr/local/bin/claude" "$REPO_DIR/.env"; then
        sed -i '' "s|CLAUDE_PATH=.*|CLAUDE_PATH=$CLAUDE_PATH|" "$REPO_DIR/.env"
        echo "      Updated CLAUDE_PATH in .env"
    fi
else
    echo "[4/5] ⚠️  claude CLI not found in PATH. Install it and update CLAUDE_PATH in .env"
fi

# 5. Detect Node path (needed for claude CLI)
NODE_PATH="$(dirname "$(which node 2>/dev/null || echo '/usr/bin/node')")"
NVM_NODE="$(ls "$HOME/.nvm/versions/node/" 2>/dev/null | sort -V | tail -1)"
if [ -n "$NVM_NODE" ]; then
    NODE_PATH="$HOME/.nvm/versions/node/$NVM_NODE/bin"
fi

# 6. Create launchctl plist
echo "[5/5] Creating launchctl plist at $PLIST_PATH..."
cat > "$PLIST_PATH" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${REPO_DIR}/venv/bin/python</string>
        <string>${REPO_DIR}/bridge.py</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>${NODE_PATH}:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>WorkingDirectory</key>
    <string>${REPO_DIR}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${REPO_DIR}/bridge.log</string>
    <key>StandardErrorPath</key>
    <string>${REPO_DIR}/bridge.error.log</string>
</dict>
</plist>
EOF

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env:  open $REPO_DIR/.env"
echo "     - Set TELEGRAM_BOT_TOKEN (from @BotFather)"
echo "     - Set ALLOWED_USERS (your Telegram user ID — get it from @userinfobot)"
echo "     - Set CLAUDE_PATH if not auto-detected"
echo ""
echo "  2. Start the bot:"
echo "     launchctl load $PLIST_PATH"
echo ""
echo "  3. Check it's running:"
echo "     ps aux | grep bridge.py | grep -v grep"
echo ""
echo "  4. View logs:"
echo "     tail -f $REPO_DIR/bridge.error.log"
echo ""
echo "  5. Restart after changes:"
echo "     launchctl stop $PLIST_NAME && launchctl start $PLIST_NAME"
