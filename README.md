# Telegram-Claude Bridge

A Telegram bot that pipes voice, text, images, and documents into Claude Code CLI, with multi-session project routing via forum topics.

## What it does

- **Text/voice messages** → sent to Claude Code (voice transcribed locally with Whisper)
- **Photos/screenshots** → Claude reads and analyzes them
- **Documents/PDFs** → Claude reads and analyzes them
- **Forum topics** → each topic routes to its own isolated Claude session
- **`/plan <request>`** → Claude describes what it would do before executing

## Why this exists

Claude Code normally requires you to be at your computer. This bridge lets you use it from anywhere via Telegram — on your phone, on the go, or from any device.

**No API key needed.** Uses the Claude Code CLI with your existing Claude subscription (Pro or Max). Not charged per token.

## Step 1 — Create your Telegram bot

1. Open Telegram and search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g. "My Claude Bridge")
4. Choose a username ending in `bot` (e.g. `myclaudebridge_bot`)
5. BotFather gives you a token like `123456789:ABCdef...` — **copy it**

**Find your Telegram user ID:**
1. Message **@userinfobot** on Telegram
2. It replies with your user ID (a number like `301192776`)
3. You'll need this for `ALLOWED_USERS` in `.env`

**Optional — for group/forum use:**
- In BotFather: `/mybots` → your bot → Bot Settings → Group Privacy → **Disable**
  (required so the bot can read regular messages in groups, not just commands)

---

## Step 2 — Install Claude Code

Install and authenticate the Claude Code CLI on your Mac:

```bash
npm install -g @anthropic-ai/claude-code
claude   # opens browser to authenticate with your Claude subscription
```

Requires a Claude Pro or Max subscription.

---

## Step 3 — Install the bridge

```bash
git clone https://github.com/vlrmzz/telegram-claude-bridge
cd telegram-claude-bridge
./setup.sh
```

`setup.sh` will:
- Create a Python venv and install dependencies
- Copy `.env.example` → `.env`
- Auto-detect your `claude` CLI path
- Create a launchctl plist so the bot runs on login and auto-restarts

Then edit `.env` with your tokens and start:

```bash
launchctl load ~/Library/LaunchAgents/com.<yourusername>.telegram-claude-bridge.plist
```

## Configuration

```env
TELEGRAM_BOT_TOKEN=   # from @BotFather
ALLOWED_USERS=        # your Telegram user ID (get from @userinfobot)
CLAUDE_PATH=          # full path to claude binary (auto-detected by setup.sh)
CLAUDE_MODEL=sonnet   # sonnet, opus, haiku
CLAUDE_TIMEOUT=300    # seconds
WHISPER_MODEL=base    # base, small, medium, large
OPENROUTER_API_KEY=   # optional, for web search via Perplexity
```

## Requirements

- macOS (launchctl for auto-start; Linux: adapt to systemd)
- Python 3.10+
- [Claude Code CLI](https://claude.ai/code) installed and authenticated
- A Telegram bot token from [@BotFather](https://t.me/botfather)

## Commands

| Command | Description |
|---|---|
| `/plan <request>` | Show what Claude would do, then Execute/Cancel |
| `/setup <name>` | Map current forum topic to a project |
| `/setup list` | Show all topic→project mappings |
| `/use <project>` | Switch active project (DM mode) |
| `/sessions` | List available projects |
| `/reset` | Start a new session |
| `/bash <cmd>` | Run a shell command directly |
| `/search <query>` | Web search via Perplexity |
| `/voice on\|off` | Toggle voice responses |
| `/session` | Show current session ID |

## Forum topics (multi-session)

Create a Telegram forum/supergroup with topics. Each topic maps to its own isolated Claude session:

1. Create a topic in your forum group
2. Send `/setup <project-name>` inside that topic
3. Messages in that topic route to a dedicated Claude session for that project

Session data is stored in `project_sessions.json`.

## Baby tracker (optional)

A separate bot (`baby_bot.py`) for tracking baby feeding, diapers, sleep, etc. Uses its own bot token (`BABY_BOT_TOKEN`) and allowed users list (`BABY_ALLOWED_USERS`).

To run it as a persistent service:

```bash
# setup.sh does not create this automatically — copy the main plist and adapt
cp ~/Library/LaunchAgents/com.<user>.telegram-claude-bridge.plist \
   ~/Library/LaunchAgents/com.<user>.baby-bot.plist
# Edit: change Label, ProgramArguments (bridge.py → baby_bot.py), log paths
launchctl load ~/Library/LaunchAgents/com.<user>.baby-bot.plist
```

## Operations

```bash
# Restart
launchctl stop com.<user>.telegram-claude-bridge
launchctl start com.<user>.telegram-claude-bridge

# Logs
tail -f bridge.error.log

# Check running
ps aux | grep bridge.py | grep -v grep
```

## Multiple machines

Each machine needs its own bot token. Steps per machine:
1. Create a new bot via @BotFather
2. Clone repo, run `./setup.sh`
3. Set `TELEGRAM_BOT_TOKEN` and `ALLOWED_USERS` in `.env`
4. Load the plist

## Security

- `ALLOWED_USERS` whitelist — only listed Telegram user IDs can interact
- Bot runs with `--dangerously-skip-permissions` — Claude has full filesystem access
- Keep your Telegram account secured with 2FA
- Never commit `.env`
- Run `chmod 600 .env` after setup

## License

MIT
