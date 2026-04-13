# Telegram Claude Bridge

A Telegram bot that pipes voice and text messages into Claude Code, with conversation continuity and two-step tool approval.

## What it does

- Send a **voice message** → transcribed locally with Whisper → sent to Claude Code
- Send a **text message** → sent directly to Claude Code
- Send a **photo/screenshot** → Claude reads and analyzes it
- Claude responds in Telegram
- If Claude needs to use tools (read files, run commands, edit code), the bot shows what tools it wants and asks for approval before executing

## How the approval flow works

1. You send a message
2. Claude runs in default mode — tools are blocked
3. If Claude wanted tools, the bot shows them as Telegram buttons:
   ```
   🔧 Claude wants to use 2 tool(s):
   1. Read  {"file_path": "/Users/you/project/main.py"}
   2. Edit  {"file_path": "/Users/you/project/main.py", ...}
   Approve execution?
   [✅ Approve] [❌ Reject]
   ```
4. Tap **Approve** → Claude re-runs with full permissions and executes
5. Tap **Reject** → Claude's text-only response is shown instead

## Why this exists

Claude Code normally requires you to be at your computer. This bridge lets you use it from anywhere via Telegram — on your phone, on the go, or from any device.

**No API key needed.** The bridge uses the Claude Code CLI, which runs under your existing Claude subscription (Pro or Max). You're not charged per token — it uses the same session as if you were typing in your terminal.

## Requirements

- Python 3.10+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated with your Claude subscription
- A Telegram bot token (from [@BotFather](https://t.me/botfather))
- Optional: faster-whisper for voice message support

## Installation

```bash
git clone https://github.com/yourname/telegram-claude-bridge
cd telegram-claude-bridge
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

```env
TELEGRAM_BOT_TOKEN=your-telegram-bot-token
ALLOWED_USERS=123456789,987654321   # your Telegram user ID(s)
WHISPER_MODEL=base                  # base, small, medium, large
CLAUDE_MODEL=sonnet                 # sonnet, opus, haiku
CLAUDE_TIMEOUT=300                  # seconds before timeout
CLAUDE_PATH=/usr/local/bin/claude   # full path to claude CLI binary
```

To find your Telegram user ID, message [@userinfobot](https://t.me/userinfobot).

To find your Claude CLI path: `which claude`

## Running

```bash
source venv/bin/activate
python bridge.py
```

To run in the background:

```bash
nohup python bridge.py > bridge.log 2>&1 &
```

## Commands

| Command | Description |
|---|---|
| `/start` | Show welcome message |
| `/session` | Show current session ID |
| `/resume <id>` | Resume a specific session |
| `/reset` | Start a fresh conversation |
| `/close` | Same as reset |
| `/bash <cmd>` | Run a shell command directly and return output |

## Managing Claude Code permissions (settings.json)

By default Claude Code prompts for approval on every tool use via a desktop UI. When running headless (no desktop), you can pre-approve specific tools in Claude Code's settings files so the bridge never blocks waiting for a GUI prompt.

### Where the files live

| File | Scope |
|---|---|
| `~/.claude/settings.json` | Global — applies to all projects |
| `<project>/.claude/settings.local.json` | Per-project — not committed to git |

### Structure

```json
{
  "permissions": {
    "allow": [
      "Read(*)",
      "Edit(*)",
      "Write(*)",
      "Bash(git status)",
      "Bash(git diff*)",
      "Bash(npm run*)"
    ],
    "deny": []
  }
}
```

### Common permission patterns

```json
"Read(*)"           // allow reading any file
"Edit(*)"           // allow editing any file
"Write(*)"          // allow writing any file
"Bash(*)"           // allow ALL bash commands (use carefully)
"Bash(git *)"       // allow any git command
"Bash(npm run *)"   // allow npm scripts only
"WebFetch(*)"       // allow fetching any URL
```

### Recommended setup for the bridge

Add to `~/.claude/settings.json` to avoid desktop approval prompts for safe read operations:

```json
{
  "permissions": {
    "allow": [
      "Read(*)",
      "Glob(*)",
      "Grep(*)"
    ],
    "deny": []
  }
}
```

For write/execute operations, keep using the Telegram approval flow (the bridge's two-step system) rather than pre-approving everything globally.

### Per-project settings

For a specific project you trust fully, create `.claude/settings.local.json` inside that project:

```json
{
  "permissions": {
    "allow": [
      "Read(*)",
      "Edit(*)",
      "Write(*)",
      "Bash(npm run *)",
      "Bash(git *)"
    ]
  }
}
```

This is gitignored by default (`.local.json`) so your personal approvals don't leak into the repo.

## Voice messages

Voice transcription uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) running locally — no cloud STT, no data sent anywhere. Set `WHISPER_MODEL` in `.env`:

- `base` — fastest, good enough for most use
- `small` — better accuracy, slightly slower
- `medium` / `large` — best accuracy, requires more RAM

## Running multiple instances

Each instance needs its own `.env` with a different bot token. Useful for managing multiple machines or projects from one Telegram account.

```
Machine 1 (personal):    BOT_TOKEN=token1
Machine 2 (work server): BOT_TOKEN=token2
Machine 3 (side project): BOT_TOKEN=token3
```

## Tips

- Use [Tailscale](https://tailscale.com) to keep the bridge reachable from anywhere without port forwarding
- The bridge maintains session continuity via `sessions.json` — your conversation history persists across restarts
- Sessions survive bridge restarts; use `/reset` to start fresh

## Security

- `ALLOWED_USERS` whitelist — only specified Telegram user IDs can interact with the bot
- Two-step approval — you explicitly approve tool usage before execution
- Voice files are downloaded to a temp file, transcribed, then immediately deleted
- Never commit `.env` — it contains your bot token

## License

MIT
