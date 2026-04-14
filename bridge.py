#!/usr/bin/env python3
"""Telegram Voice → Claude Code bridge.

Receive voice messages on Telegram, transcribe locally with faster-whisper,
and pipe them into Claude Code (using your Max subscription) with full
conversation continuity via --resume.

Two-step tool approval:
1. Claude runs in default permission mode (tools get denied)
2. If Claude wanted tools, the bot shows them on Telegram for approval
3. If approved, Claude re-runs with --dangerously-skip-permissions
4. Tool usage is streamed to Telegram in real-time
"""

import asyncio
import json
import logging
import os
import tempfile
import uuid
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USERS = {
    int(uid.strip())
    for uid in os.getenv("ALLOWED_USERS", "").split(",")
    if uid.strip()
}
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "sonnet")
CLAUDE_PATH = os.getenv("CLAUDE_PATH", "claude")
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

# Per-chat state
SESSIONS_FILE = Path(__file__).parent / "sessions.json"
chat_locks: dict[int, asyncio.Lock] = {}

# Pending approval: request_id -> {chat_id, prompt, tools, fallback_text}
pending_approvals: dict[str, dict] = {}

# Store the bot application globally
_app: Application | None = None


def _load_sessions() -> dict[int, str]:
    if SESSIONS_FILE.exists():
        try:
            data = json.loads(SESSIONS_FILE.read_text())
            return {int(k): v for k, v in data.items()}
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_sessions():
    SESSIONS_FILE.write_text(json.dumps(
        {str(k): v for k, v in sessions.items()}, indent=2
    ))


sessions: dict[int, str] = _load_sessions()

# ---------------------------------------------------------------------------
# Whisper STT (lazy-loaded)
# ---------------------------------------------------------------------------
_whisper_model = None


def get_whisper_model():
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel
        log.info("Loading Whisper model '%s' ...", WHISPER_MODEL)
        _whisper_model = WhisperModel(WHISPER_MODEL, compute_type="int8")
        log.info("Whisper model ready.")
    return _whisper_model


def transcribe_audio(file_path: str) -> str:
    model = get_whisper_model()
    segments, _ = model.transcribe(file_path, beam_size=5)
    return " ".join(seg.text.strip() for seg in segments)


# ---------------------------------------------------------------------------
# Claude Code interface
# ---------------------------------------------------------------------------
def _truncate(text: str, max_len: int = 500) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "…"


async def _run_claude(prompt: str, session_id: str | None, skip_permissions: bool,
                      max_turns: int = 10) -> tuple[list[dict], str | None, str]:
    """Run claude CLI and return (parsed_messages, session_id, final_text)."""
    cmd = [CLAUDE_PATH, "-p", prompt,
           "--output-format", "stream-json", "--verbose"]

    if session_id:
        cmd += ["--resume", session_id]
    if CLAUDE_MODEL:
        cmd += ["--model", CLAUDE_MODEL]
    if skip_permissions:
        cmd += ["--dangerously-skip-permissions"]
    cmd += ["--max-turns", str(max_turns)]

    log.info("Running claude (skip_perms=%s): %s", skip_permissions, " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=CLAUDE_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        raise asyncio.TimeoutError(f"Claude timed out after {CLAUDE_TIMEOUT}s")

    if stderr:
        log.warning("Claude stderr: %s", stderr.decode()[:500])
    if proc.returncode != 0:
        log.error("Claude exited with code %s", proc.returncode)

    messages = []
    new_session_id = session_id
    final_text = ""

    for line in stdout.decode().strip().split("\n"):
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
            messages.append(msg)

            if msg.get("session_id"):
                new_session_id = msg["session_id"]

            if msg.get("type") == "result" and msg.get("result"):
                final_text = msg["result"]
            elif msg.get("type") == "assistant":
                content = msg.get("message", {}).get("content", [])
                for block in content:
                    if block.get("type") == "text":
                        final_text = block["text"]

        except json.JSONDecodeError:
            continue

    return messages, new_session_id, final_text


def _extract_tool_calls(messages: list[dict]) -> list[dict]:
    """Extract tool_use blocks from assistant messages."""
    tools = []
    for msg in messages:
        if msg.get("type") != "assistant":
            continue
        content = msg.get("message", {}).get("content", [])
        for block in content:
            if block.get("type") == "tool_use":
                tools.append({
                    "name": block["name"],
                    "input": block.get("input", {}),
                })
    return tools


def _format_tool_list(tools: list[dict]) -> str:
    """Format tool calls for Telegram display."""
    lines = []
    for i, tool in enumerate(tools, 1):
        name = tool["name"]
        inp = tool.get("input", {})
        if isinstance(inp, dict):
            display = json.dumps(inp, indent=2, ensure_ascii=False)
        else:
            display = str(inp)
        lines.append(f"{i}. *{name}*\n```\n{_truncate(display, 300)}\n```")
    return "\n".join(lines)


async def _execute_approved(chat_id: int, prompt: str, approval: dict):
    """Execute Claude with full permissions (called after approval)."""
    log.info("Executing with permissions — chat %s", chat_id)
    await _app.bot.send_message(chat_id=chat_id, text="⏳ Executing...")

    exec_messages, new_session_id, exec_result = await _run_claude(
        prompt=prompt,
        session_id=approval.get("session_id_before"),
        skip_permissions=True,
        max_turns=10,
    )

    if new_session_id:
        sessions[chat_id] = new_session_id
        _save_sessions()

    # Notify what tools were used
    tools_used = _extract_tool_calls(exec_messages)
    if tools_used:
        tool_names = [t["name"] for t in tools_used]
        summary = ", ".join(f"`{n}`" for n in tool_names)
        await _app.bot.send_message(
            chat_id=chat_id,
            text=f"⚡ Tools used: {summary}",
            parse_mode="Markdown",
        )

    # Send result
    result = exec_result or "Claude returned an empty response."
    for chunk in split_message(result):
        await _app.bot.send_message(chat_id=chat_id, text=chunk)


async def handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Approve/Reject button taps."""
    query = update.callback_query
    await query.answer()

    data = query.data
    log.info("Callback received: %s", data)

    if not (data.startswith("approve_") or data.startswith("reject_")):
        return

    parts = data.split("_", 1)
    action = parts[0]
    request_id = parts[1]

    if request_id not in pending_approvals:
        await query.edit_message_text("⚠️ This approval request has expired.")
        return

    approval = pending_approvals.pop(request_id)
    chat_id = approval["chat_id"]
    approved = action == "approve"

    if approved:
        await query.edit_message_text("✅ Approved — executing...")
        # Run execution in background so we don't block the callback
        asyncio.create_task(_execute_approved(chat_id, approval["prompt"], approval))
    else:
        await query.edit_message_text("❌ Rejected.")
        fallback = approval.get("fallback_text", "")
        if fallback:
            await _app.bot.send_message(
                chat_id=chat_id,
                text=f"Claude's response without tools:\n\n{fallback}",
            )


async def send_to_claude(text: str, chat_id: int):
    """Two-step flow: try without permissions, ask approval if tools needed."""
    session_id = sessions.get(chat_id)

    # Step 1: Run Claude in default mode (tools will be denied)
    log.info("Step 1: Planning — chat %s", chat_id)
    messages, new_session_id, result_text = await _run_claude(
        prompt=text,
        session_id=session_id,
        skip_permissions=False,
        max_turns=10,
    )

    # Check if Claude tried to use any tools
    denied_tools = _extract_tool_calls(messages)

    if not denied_tools:
        # No tools needed — save session and return the direct answer
        if new_session_id:
            sessions[chat_id] = new_session_id
            _save_sessions()
        return result_text or "Claude returned an empty response."

    # Tools needed — do NOT save the session yet (Step 2 will save it after execution)
    # Step 2: Show tools and ask for approval (non-blocking)
    request_id = str(uuid.uuid4())[:8]
    log.info("Step 2: Asking approval for %d tool(s) [%s]", len(denied_tools), request_id)

    pending_approvals[request_id] = {
        "chat_id": chat_id,
        "prompt": text,
        "tools": denied_tools,
        "fallback_text": result_text,
        "session_id_before": session_id,  # resume from before Step 1, not after
    }

    tool_text = _format_tool_list(denied_tools)
    msg_text = (
        f"🔧 *Claude wants to use {len(denied_tools)} tool(s):*\n\n"
        f"{tool_text}\n\n"
        f"Approve execution?"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve_{request_id}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{request_id}"),
        ]
    ])

    await _app.bot.send_message(
        chat_id=chat_id,
        text=msg_text,
        parse_mode="Markdown",
        reply_markup=keyboard,
    )

    # Return None to signal that we're waiting for approval
    return None


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------
TELEGRAM_MSG_LIMIT = 4096


def split_message(text: str) -> list[str]:
    if len(text) <= TELEGRAM_MSG_LIMIT:
        return [text]
    chunks = []
    while text:
        if len(text) <= TELEGRAM_MSG_LIMIT:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, TELEGRAM_MSG_LIMIT)
        if cut == -1:
            cut = text.rfind("\n", 0, TELEGRAM_MSG_LIMIT)
        if cut == -1:
            cut = TELEGRAM_MSG_LIMIT
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def is_allowed(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


def get_lock(chat_id: int) -> asyncio.Lock:
    if chat_id not in chat_locks:
        chat_locks[chat_id] = asyncio.Lock()
    return chat_locks[chat_id]


# ---------------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    await update.message.reply_text(
        "Hey! Send me a voice or text message and I'll pass it to Claude Code.\n\n"
        "When Claude needs to use tools (read files, run commands, etc.), "
        "I'll ask for your approval first.\n\n"
        "Commands:\n"
        "/reset - Start a new conversation\n"
        "/session - Show current session info\n"
        "/resume <session_id> - Resume a specific session\n"
        "/close - End current session"
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    sessions.pop(chat_id, None)
    _save_sessions()
    await update.message.reply_text("Session cleared. Next message starts a fresh conversation.")


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    sid = sessions.get(chat_id)
    if sid:
        await update.message.reply_text(f"Session: `{sid}`", parse_mode="Markdown")
    else:
        await update.message.reply_text("No active session. Send a message to start one.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Usage: `/resume <session_id>`", parse_mode="Markdown")
        return
    session_id = context.args[0]
    sessions[chat_id] = session_id
    _save_sessions()
    await update.message.reply_text(f"Resumed session: `{session_id}`", parse_mode="Markdown")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    chat_id = update.effective_chat.id

    await update.message.reply_chat_action("typing")
    voice = update.message.voice or update.message.audio
    file = await voice.get_file()

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
        await file.download_to_drive(tmp_path)

    try:
        transcript = await asyncio.to_thread(transcribe_audio, tmp_path)
    finally:
        os.unlink(tmp_path)

    if not transcript.strip():
        await update.message.reply_text("Couldn't hear anything. Try again?")
        return

    await update.message.reply_text(f"Heard: {transcript}", do_quote=True)

    await update.message.reply_chat_action("typing")
    try:
        response = await send_to_claude(transcript, chat_id)
    except asyncio.TimeoutError:
        await update.message.reply_text(f"⏱ Timed out after {CLAUDE_TIMEOUT}s — the command took too long.")
        return

    # If None, we're waiting for approval (handled by callback)
    if response is not None:
        for chunk in split_message(response):
            await update.message.reply_text(chunk)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    text = update.message.text

    await update.message.reply_chat_action("typing")
    try:
        response = await send_to_claude(text, chat_id)
    except asyncio.TimeoutError:
        await update.message.reply_text(f"⏱ Timed out after {CLAUDE_TIMEOUT}s — the command took too long.")
        return

    # If None, we're waiting for approval (handled by callback)
    if response is not None:
        for chunk in split_message(response):
            await update.message.reply_text(chunk)


async def handle_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo/screenshot messages — save locally and pass path to Claude."""
    if not is_allowed(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    caption = update.message.caption or "Please analyze this screenshot."

    # Download the highest resolution photo
    photo = update.message.photo[-1]
    file = await photo.get_file()

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
        await file.download_to_drive(tmp_path)

    await update.message.reply_chat_action("typing")

    # Run Claude directly with permissions so it can Read the image file
    prompt = f"{caption}\n\nThe image is saved at: {tmp_path} — use the Read tool to view it."
    messages, new_session_id, result_text = await _run_claude(
        prompt=prompt,
        session_id=sessions.get(chat_id),
        skip_permissions=True,
        max_turns=3,
    )

    os.unlink(tmp_path)

    if new_session_id:
        sessions[chat_id] = new_session_id
        _save_sessions()

    response = result_text or "Claude returned an empty response."
    for chunk in split_message(response):
        await update.message.reply_text(chunk)


async def cmd_bash(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run a bash command directly and return output."""
    if not is_allowed(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: `/bash <command>`", parse_mode="Markdown")
        return

    command = " ".join(context.args)
    log.info("Direct bash: %s", command)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = stdout.decode().strip() or stderr.decode().strip() or "(no output)"
    except asyncio.TimeoutError:
        output = "⚠️ Command timed out after 30 seconds."
    except Exception as e:
        output = f"⚠️ Error: {e}"

    for chunk in split_message(f"```\n{output}\n```"):
        await update.message.reply_text(chunk, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global _app

    if not TELEGRAM_BOT_TOKEN:
        print("Set TELEGRAM_BOT_TOKEN in .env or environment")
        return

    _app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Register callback handler FIRST so it has priority
    _app.add_handler(CallbackQueryHandler(handle_approval_callback))
    _app.add_handler(CommandHandler("start", cmd_start))
    _app.add_handler(CommandHandler("reset", cmd_reset))
    _app.add_handler(CommandHandler("session", cmd_session))
    _app.add_handler(CommandHandler("resume", cmd_resume))
    _app.add_handler(CommandHandler("close", cmd_reset))
    _app.add_handler(CommandHandler("new", cmd_reset))
    _app.add_handler(CommandHandler("bash", cmd_bash))
    _app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    _app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bridge started. Listening for Telegram messages...")
    _app.run_polling()


if __name__ == "__main__":
    main()
