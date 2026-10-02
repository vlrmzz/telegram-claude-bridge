# Telegram-Claude Bridge

A small Telegram bot that pipes text, voice, photos and documents into the [Claude Code](https://claude.com/claude-code) CLI running on your own machine, so you can talk to it from your phone.

> **Status: no longer maintained.** I built this in spring 2026 as a personal tool and have since stopped using it. It worked for me on macOS at the time; it is left here as a reference, not as a supported project. Read the [Security](#security) section before running it.

## What it does

- **Text and voice** → sent to Claude Code (voice is transcribed locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper))
- **Photos and documents** → saved to a temp file that Claude reads and analyses, with an optional "save to captures" step
- **Forum topics** → each topic in a Telegram forum group gets its own isolated Claude session
- **`/plan <request>`** → Claude describes what it would do, then you tap Execute or Cancel
- **Spoken replies** (optional) → responses read back as Telegram voice messages

## How it works

```
Telegram ──▶ bridge.py (long polling) ──▶ claude -p "<prompt>" --resume <session> ──▶ reply
```

It is a single file, [bridge.py](bridge.py). Each incoming message spawns one headless `claude -p` run with `--output-format stream-json`; the bridge keeps the returned session ID per chat (or per project) and passes it back with `--resume`, which is what gives the conversation continuity. Session IDs and topic mappings live in small JSON files next to the script.

It drives the Claude Code CLI you are already logged into, so there is no separate API key to configure.

## Setup

Requirements: macOS, Python 3.10+, the Claude Code CLI installed and authenticated, and `ffmpeg` on your `PATH` if you want spoken replies. On Linux everything but `setup.sh` should work; swap launchctl for systemd.

**1. Create a Telegram bot.** Message [@BotFather](https://t.me/botfather), send `/newbot`, and copy the token it gives you. Message [@userinfobot](https://t.me/userinfobot) to get your numeric user ID. For forum/group use, also disable Group Privacy for the bot (BotFather → `/mybots` → Bot Settings) so it can read non-command messages.

**2. Install the bridge.**

```bash
git clone https://github.com/vlrmzz/telegram-claude-bridge
cd telegram-claude-bridge
./setup.sh
```

`setup.sh` creates a venv, installs dependencies, copies `.env.example` to `.env`, detects your `claude` path, and writes a launchd plist so the bot starts on login and restarts if it dies.

**3. Fill in `.env` and start it.**

```bash
launchctl load ~/Library/LaunchAgents/com.<yourusername>.telegram-claude-bridge.plist
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | Token from @BotFather |
| `ALLOWED_USERS` | — | Comma-separated Telegram user IDs. Required: the bridge refuses to start without it |
| `CLAUDE_PATH` | `claude` | Full path to the `claude` binary (auto-detected by `setup.sh`) |
| `CLAUDE_MODEL` | `sonnet` | Model alias passed to `claude --model` |
| `CLAUDE_TIMEOUT` | `300` | Seconds before a run is killed |
| `WHISPER_MODEL` | `base` | faster-whisper model size (`base`, `small`, `medium`, `large`) |
| `PROJECTS_DIR` | `~/resources` | Directory of projects and saved captures (see below) |
| `TTS_VOICE` | `en-US-GuyNeural` | [edge-tts](https://github.com/rany2/edge-tts) voice for spoken replies |
| `OPENROUTER_API_KEY` | — | Optional, enables `/search` (Perplexity Sonar via OpenRouter) |

## Commands

| Command | Description |
|---|---|
| `/plan <request>` | Show what Claude would do, then Execute/Cancel |
| `/reset` (also `/new`, `/close`) | Start a new session |
| `/session` | Show the current session ID |
| `/resume <session_id>` | Attach this chat to an existing Claude Code session |
| `/use <project>` | Switch the active project in a direct chat (`/use off` to clear) |
| `/sessions` | List available projects |
| `/setup <project>` | Map the current forum topic to a project (`/setup list`, `/setup remove`) |
| `/search <query>` | Web search, with the results summarised by Claude |
| `/find <query>` | Search saved captures |
| `/bash <cmd>` | Run a shell command directly, bypassing Claude |
| `/voice on\|off` | Toggle spoken replies |

## Projects and forum topics

A *project* is a subdirectory of `PROJECTS_DIR` containing an `AGENTS.md`. When a chat is routed to a project, that file is prepended to every prompt and the project gets its own Claude session, separate from the default one.

There are two ways to route:

- **Direct chat:** `/use <project>` switches the active project.
- **Forum group:** create a Telegram group with topics enabled. New topics are registered automatically (with an offer to scaffold an `AGENTS.md`); existing ones can be mapped with `/setup <project>` from inside the topic.

Photos and documents you choose to keep are copied to `PROJECTS_DIR/captures/<category>/` with a Markdown sidecar holding Claude's analysis, which is what `/find` searches.

## Operations

```bash
# Restart
launchctl stop com.<user>.telegram-claude-bridge
launchctl start com.<user>.telegram-claude-bridge

# Logs
tail -f bridge.error.log
```

## Security

This bot is remote code execution on your machine by design. Understand what that means before running it:

- Claude runs with `--dangerously-skip-permissions`, so it can read, write and execute anything your user account can. The "describe writes and wait for confirmation" behaviour is only an instruction in the prompt, not an enforced sandbox.
- `/bash` runs arbitrary shell commands with no confirmation at all.
- The only access control is the `ALLOWED_USERS` whitelist, so anyone who takes over a whitelisted Telegram account controls the machine. Enable two-step verification on that account.
- Use a dedicated bot token per machine, keep `.env` out of version control, and keep it `chmod 600` (`setup.sh` does this when it creates the file).

## License

[MIT](LICENSE)
