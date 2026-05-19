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
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

import re as _re

# Keywords that trigger auto web search
# Explicit search phrases — must match as a phrase, not a lone word
_SEARCH_TRIGGER_PHRASES = [
    # English
    r'search for\b', r'look up\b', r'find out\b', r'what.s happening',
    r'latest news', r'current news', r'search the web',
    # Italian
    r'cerca su internet', r'cercami\b', r'cosa sta succedendo',
    r'ultime notizie', r'cerca online',
    # Spanish/Portuguese
    r'busca en internet', r'buscar en la web', r'noticias de hoy',
]

_SEARCH_TRIGGER_RE = _re.compile(
    '|'.join(_SEARCH_TRIGGER_PHRASES),
    _re.IGNORECASE
)


def _needs_search(text: str) -> bool:
    """Return True if the message explicitly requests a web search."""
    return bool(_SEARCH_TRIGGER_RE.search(text))


async def perplexity_search(query: str) -> str | None:
    """Search the web using Perplexity sonar via OpenRouter. Returns formatted results or None."""
    if not OPENROUTER_API_KEY:
        log.warning("OPENROUTER_API_KEY not set — skipping search")
        return None
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)
        resp = await client.chat.completions.create(
            model="perplexity/sonar",
            messages=[{"role": "user", "content": query}],
            max_tokens=800,
        )
        raw = resp.model_dump()
        text = raw.get("choices", [{}])[0].get("message", {}).get("content", "").strip()

        # Extract citations
        citations = []
        for choice in raw.get("choices", []):
            for ann in choice.get("message", {}).get("annotations", []):
                if ann.get("type") == "url_citation":
                    uc = ann.get("url_citation", {})
                    if uc.get("url"):
                        citations.append(f"- [{uc.get('title', uc['url'])}]({uc['url']})")

        result = f"**Web search results:**\n{text}"
        if citations:
            result += "\n\n**Sources:**\n" + "\n".join(citations[:5])
        return result
    except Exception as e:
        log.warning("Perplexity search failed: %s", e)
        return None


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # suppress token-exposing API URLs

# Per-chat state
SESSIONS_FILE = Path(__file__).parent / "sessions.json"
PROJECT_SESSIONS_FILE = Path(__file__).parent / "project_sessions.json"
ACTIVE_PROJECT_FILE = Path(__file__).parent / "active_project.json"
TOPICS_FILE = Path(__file__).parent / "topics.json"
WIKI_DIR = Path.home() / "resources"
chat_locks: dict[int, asyncio.Lock] = {}

# Pending approval: request_id -> {chat_id, prompt, tools, fallback_text}
pending_approvals: dict[str, dict] = {}

# Pending saves: save_id -> {tmp_path, response, original_name}
pending_saves: dict[str, dict] = {}

# Recently sent photos: (chat_id, file_path) -> timestamp, to prevent duplicate sends
_recent_sends: dict[tuple, float] = {}
_SEND_COOLDOWN = 60  # seconds

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


def _load_project_sessions() -> dict[str, str]:
    """Load named project sessions: '{chat_id}_{project}' -> session_id"""
    if PROJECT_SESSIONS_FILE.exists():
        try:
            return json.loads(PROJECT_SESSIONS_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_project_sessions():
    PROJECT_SESSIONS_FILE.write_text(json.dumps(project_sessions, indent=2))


def _load_active_projects() -> dict[int, str]:
    """Load active project per chat_id"""
    if ACTIVE_PROJECT_FILE.exists():
        try:
            data = json.loads(ACTIVE_PROJECT_FILE.read_text())
            return {int(k): v for k, v in data.items()}
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_active_projects():
    ACTIVE_PROJECT_FILE.write_text(json.dumps(
        {str(k): v for k, v in active_projects.items()}, indent=2
    ))


def _load_topics() -> dict[str, str]:
    """Load topic mappings: '{chat_id}_{thread_id}' -> project_name"""
    if TOPICS_FILE.exists():
        try:
            return json.loads(TOPICS_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def _save_topics():
    TOPICS_FILE.write_text(json.dumps(topic_map, indent=2))


def _get_project_context(project: str) -> str | None:
    """Load AGENTS.md for a project as context string."""
    agents_file = WIKI_DIR / project / "AGENTS.md"
    if agents_file.exists():
        return agents_file.read_text()
    return None


def _list_projects() -> list[str]:
    """List available projects (subdirs of WIKI_DIR with AGENTS.md)."""
    if not WIKI_DIR.exists():
        return []
    return sorted(
        d.name for d in WIKI_DIR.iterdir()
        if d.is_dir() and (d / "AGENTS.md").exists()
    )


def _sanitize_project_name(name: str) -> str:
    """Convert a topic name to a valid project key."""
    import re
    key = name.lower().replace(" ", "_").replace("-", "_")
    return re.sub(r'[^\w]', '', key)


def _resolve_project(chat_id: int, thread_id: int | None) -> str | None:
    """Return project name for this chat+thread, or None for default session.

    Priority:
    1. topic_map entry for (chat_id, thread_id) — forum topic mapping
    2. active_projects entry for chat_id — legacy /use <project> fallback
    """
    if thread_id is not None:
        key = f"{chat_id}_{thread_id}"
        if key in topic_map:
            return topic_map[key]
    return active_projects.get(chat_id)


sessions: dict[int, str] = _load_sessions()
project_sessions: dict[str, str] = _load_project_sessions()
active_projects: dict[int, str] = _load_active_projects()
topic_map: dict[str, str] = _load_topics()

# Per-chat voice mode (True = respond with audio)
voice_enabled: dict[int, bool] = {}

FFMPEG = "/opt/homebrew/bin/ffmpeg"


async def _text_to_voice(text: str) -> str | None:
    """Generate a voice message OGG file using edge-tts. Returns path or None."""
    import edge_tts
    # Strip markdown for cleaner audio
    import re
    clean = re.sub(r'[*_`#\[\]()]', '', text)
    clean = re.sub(r'https?://\S+', 'link', clean)
    clean = clean.strip()
    if not clean:
        return None
    try:
        mp3_path = tempfile.mktemp(suffix=".mp3")
        ogg_path = tempfile.mktemp(suffix=".ogg")
        communicate = edge_tts.Communicate(clean, voice="it-IT-DiegoNeural")
        await communicate.save(mp3_path)
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, "-y", "-i", mp3_path,
            "-c:a", "libopus", "-b:a", "64k", ogg_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        os.unlink(mp3_path)
        return ogg_path if os.path.exists(ogg_path) else None
    except Exception as e:
        log.warning("TTS failed: %s", e)
        return None


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
    thread_id = approval.get("thread_id")
    log.info("Executing with permissions — chat %s thread %s", chat_id, thread_id)
    await _app.bot.send_message(chat_id=chat_id, text="⏳ Running...",
                                message_thread_id=thread_id)

    exec_messages, new_session_id, exec_result = await _run_claude(
        prompt=prompt,
        session_id=approval.get("session_id_before"),
        skip_permissions=True,
        max_turns=10,
    )

    # Save session to correct bucket (topic project or default)
    project = _resolve_project(chat_id, thread_id)
    if new_session_id:
        if project:
            project_sessions[f"{chat_id}_{project}"] = new_session_id
            _save_project_sessions()
        else:
            sessions[chat_id] = new_session_id
            _save_sessions()

    # Send result
    label = project if project else "general"
    result = f"[{label}]\n{exec_result or 'Claude returned an empty response.'}"
    for chunk in split_message(result):
        await _app.bot.send_message(chat_id=chat_id, text=chunk,
                                    message_thread_id=thread_id)


async def handle_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle Approve/Reject button taps."""
    query = update.callback_query
    await query.answer()

    data = query.data
    log.info("Callback received: %s", data)

    # Handle forum topic init buttons
    if data.startswith("initagents_") or data.startswith("initskip_"):
        parts = data.split("_", 2)
        action = parts[0]
        chat_id_str, thread_id_str = parts[1], parts[2]
        cid, tid = int(chat_id_str), int(thread_id_str)
        project = topic_map.get(f"{cid}_{tid}", "unknown")
        if action == "initagents":
            await query.edit_message_text(f"Initializing project `{project}`...", parse_mode="Markdown")
            asyncio.create_task(_init_project_agents(cid, tid, project))
        else:
            await query.edit_message_text("Skipped. Messages will still route to a dedicated session.")
        return

    # Handle save/discard for captures
    if data.startswith("save_") or data.startswith("discard_"):
        action = "save" if data.startswith("save_") else "discard"
        save_id = data[len(action)+1:]
        if save_id not in pending_saves:
            await query.edit_message_text("⚠️ Save request expired.")
            return
        entry = pending_saves.pop(save_id)
        if action == "save":
            try:
                saved_path = _save_capture(entry["tmp_path"], entry["response"], original_name=entry["original_name"])
                await query.edit_message_text(f"📁 Saved to: `{saved_path}`", parse_mode="Markdown")
            except Exception as e:
                await query.edit_message_text(f"⚠️ Save failed: {e}")
        else:
            await query.edit_message_text("🗑 Discarded.")
        # Clean up temp file
        try:
            os.unlink(entry["tmp_path"])
        except Exception:
            pass
        return

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
        await query.edit_message_text("❌ Cancelled.")


_PROMPT_PREFIX = (
    "Important: always provide a complete, fresh answer. "
    "Do not say 'already answered' or refer to previous responses — "
    "just answer fully as if for the first time.\n\n"
    "TOOL USE RULES:\n"
    "- READ-ONLY operations (reading files, listing directories, searching, summarizing): proceed directly without asking.\n"
    "- WRITE operations (editing files, creating files, deleting files, running bash/shell commands, writing data): "
    "describe exactly what you are about to do and STOP. Do not execute yet. "
    "Wait for the user to explicitly confirm with 'yes', 'do it', 'go ahead', or similar before proceeding.\n"
    "- If the user's message already contains explicit confirmation (e.g. 'go ahead and edit', 'yes do it'), you may proceed directly.\n\n"
)


async def send_to_claude(text: str, chat_id: int, thread_id: int | None = None):
    """Run Claude with full permissions and return the response."""
    project = _resolve_project(chat_id, thread_id)
    if project:
        proj_key = f"{chat_id}_{project}"
        session_id = project_sessions.get(proj_key)
    else:
        session_id = sessions.get(chat_id)

    context_prefix = ""
    if project:
        ctx = _get_project_context(project)
        if ctx:
            context_prefix = f"[Project context: {project}]\n\n{ctx}\n\n---\n\n"

    full_text = _PROMPT_PREFIX + context_prefix + text

    messages, new_session_id, result_text = await _run_claude(
        prompt=full_text,
        session_id=session_id,
        skip_permissions=True,
        max_turns=10,
    )

    if new_session_id:
        if project:
            project_sessions[f"{chat_id}_{project}"] = new_session_id
            _save_project_sessions()
        else:
            sessions[chat_id] = new_session_id
            _save_sessions()

    answer = result_text or "Claude returned an empty response."
    label = project if project else "general"
    return f"[{label}]\n{answer}"


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
        "/close - End current session\n"
        "/voice on|off - Toggle voice responses"
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    project = _resolve_project(chat_id, thread_id)
    if project:
        proj_key = f"{chat_id}_{project}"
        project_sessions.pop(proj_key, None)
        _save_project_sessions()
        await update.message.reply_text(f"Session cleared for project `{project}`.", parse_mode="Markdown")
    else:
        sessions.pop(chat_id, None)
        _save_sessions()
        await update.message.reply_text("Session cleared. Next message starts a fresh conversation.")


async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    project = _resolve_project(chat_id, thread_id)
    if project:
        sid = project_sessions.get(f"{chat_id}_{project}")
        label = f"Project `{project}`"
    else:
        sid = sessions.get(chat_id)
        label = "Default session"
    if sid:
        await update.message.reply_text(f"{label}: `{sid}`", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"{label}: no active session.")


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

    # Check for voice toggle commands in transcribed text
    import re as _re
    t_norm = _re.sub(r'[^\w\s/]', '', transcript.strip().lower().replace("-", " ").replace("_", " ")).strip()
    if t_norm in ("voice on", "barra voice on"):
        voice_enabled[chat_id] = True
        await update.message.reply_text("🔊 Voice mode ON.")
        return
    if t_norm in ("voice off", "barra voice off"):
        voice_enabled[chat_id] = False
        await update.message.reply_text("🔇 Voice mode OFF.")
        return

    await update.message.reply_text(f"Heard: {transcript}", do_quote=True)

    await update.message.reply_chat_action("typing")

    # Auto web search if transcript triggers it
    search_context = ""
    if _needs_search(transcript) and OPENROUTER_API_KEY:
        await update.message.reply_text("🔍 Searching the web...", do_quote=True)
        results = await perplexity_search(transcript)
        if results:
            search_context = f"\n\n[Web search results for context:]\n{results}\n\n"

    thread_id = update.message.message_thread_id
    try:
        response = await send_to_claude(transcript + search_context, chat_id, thread_id=thread_id)
    except asyncio.TimeoutError:
        await update.message.reply_text(f"⏱ Timed out after {CLAUDE_TIMEOUT}s — the command took too long.")
        return

    # If None, we're waiting for approval (handled by callback)
    if response is not None:
        await _send_response(update, chat_id, response)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    text = update.message.text

    # Intercept voice toggle commands sent as plain text
    import re as _re
    normalized = text.strip().lower().replace("-", " ").replace("_", " ")
    normalized = _re.sub(r'[^\w\s/]', '', normalized).strip()
    if normalized in ("voice on", "barra voice on", "/voice on"):
        voice_enabled[chat_id] = True
        await update.message.reply_text("🔊 Voice mode ON.")
        return
    if normalized in ("voice off", "barra voice off", "/voice off"):
        voice_enabled[chat_id] = False
        await update.message.reply_text("🔇 Voice mode OFF.")
        return

    await update.message.reply_chat_action("typing")

    # Auto web search if message triggers it
    search_context = ""
    if _needs_search(text) and OPENROUTER_API_KEY:
        await update.message.reply_text("🔍 Searching the web...", do_quote=True)
        results = await perplexity_search(text)
        if results:
            search_context = f"\n\n[Web search results for context:]\n{results}\n\n"

    thread_id = update.message.message_thread_id
    try:
        response = await send_to_claude(text + search_context, chat_id, thread_id=thread_id)
    except asyncio.TimeoutError:
        await update.message.reply_text(f"⏱ Timed out after {CLAUDE_TIMEOUT}s — the command took too long.")
        return

    # If None, we're waiting for approval (handled by callback)
    if response is not None:
        await _send_response(update, chat_id, response)


def _save_capture(src_path: str, analysis: str, original_name: str = "") -> str:
    """Save a captured file to ~/resources/captures/ with a sidecar .md. Returns save path."""
    import shutil
    import re as _re
    from datetime import date as _date

    # Detect category from analysis text
    text = analysis.lower()
    if any(w in text for w in ["flight", "boarding pass", "airport", "hotel booking", "reservation", "passport", "itinerary", "departure", "arrival gate"]):
        category = "travel"
    elif any(w in text for w in ["receipt", "invoice", "total", "payment", "price", "chf", "eur", "usd", "purchase", "order"]):
        category = "receipts"
    elif any(w in text for w in ["health", "medical", "doctor", "prescription", "blood", "weight", "temperature"]):
        category = "health"
    elif any(w in text for w in ["arxiv", "paper", "transformer", "neural", "model", "dataset", "benchmark", "iclr", "neurips", "research"]):
        category = "research"
    elif any(w in text for w in ["idea", "note", "todo", "reminder", "plan", "thought"]):
        category = "ideas"
    else:
        category = "general"

    # Build filename slug from analysis (first 6 words)
    words = _re.sub(r'[^\w\s]', '', analysis[:60]).split()[:6]
    slug = "-".join(w.lower() for w in words if w) or "capture"
    slug = slug[:50]

    today = _date.today().isoformat()
    suffix = Path(src_path).suffix or ".jpg"
    base_name = f"{today}-{slug}"

    # Ensure directory exists
    capture_dir = WIKI_DIR / "captures" / category
    capture_dir.mkdir(parents=True, exist_ok=True)

    # Copy file
    dest_path = capture_dir / f"{base_name}{suffix}"
    # Avoid overwriting
    counter = 1
    while dest_path.exists():
        dest_path = capture_dir / f"{base_name}-{counter}{suffix}"
        counter += 1
    shutil.copy2(src_path, dest_path)

    # Write sidecar .md
    md_path = dest_path.with_suffix(".md")
    md_path.write_text(
        f"---\ndate: {today}\ncategory: {category}\nfile: {dest_path.name}\n"
        f"original: {original_name}\n---\n\n{analysis}\n"
    )

    log.info("Capture saved: %s", dest_path)
    return str(dest_path)


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

    thread_id = update.message.message_thread_id
    project = _resolve_project(chat_id, thread_id)
    if project:
        session_id = project_sessions.get(f"{chat_id}_{project}")  # None = fresh isolated session
    else:
        session_id = sessions.get(chat_id)

    # Run Claude directly with permissions so it can Read the image file
    prompt = f"{caption}\n\nThe image is saved at: {tmp_path} — use the Read tool to view it."
    messages, new_session_id, result_text = await _run_claude(
        prompt=prompt,
        session_id=session_id,
        skip_permissions=True,
        max_turns=3,
    )

    response = result_text or "Claude returned an empty response."

    if new_session_id:
        if project:
            project_sessions[f"{chat_id}_{project}"] = new_session_id
            _save_project_sessions()
        else:
            sessions[chat_id] = new_session_id
            _save_sessions()

    for chunk in split_message(response):
        await update.message.reply_text(chunk)

    # Ask whether to save — don't auto-save
    save_id = str(uuid.uuid4())
    pending_saves[save_id] = {"tmp_path": tmp_path, "response": response, "original_name": "photo.jpg"}
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💾 Save", callback_data=f"save_{save_id}"),
        InlineKeyboardButton("🗑 Discard", callback_data=f"discard_{save_id}"),
    ]])
    await update.message.reply_text("Save this photo to captures?", reply_markup=keyboard)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle document/file messages — save locally and pass path to Claude."""
    if not is_allowed(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    doc = update.message.document
    caption = update.message.caption or f"Please analyze this file: {doc.file_name}"

    file = await doc.get_file()
    suffix = Path(doc.file_name).suffix if doc.file_name else ""

    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp_path = tmp.name
        await file.download_to_drive(tmp_path)

    await update.message.reply_chat_action("typing")

    thread_id = update.message.message_thread_id
    project = _resolve_project(chat_id, thread_id)
    if project:
        session_id = project_sessions.get(f"{chat_id}_{project}")  # None = fresh isolated session
    else:
        session_id = sessions.get(chat_id)

    prompt = f"{caption}\n\nThe file is saved at: {tmp_path} — use the Read tool to view it."
    try:
        messages, new_session_id, result_text = await _run_claude(
            prompt=prompt,
            session_id=session_id,
            skip_permissions=True,
            max_turns=5,
        )
    except asyncio.TimeoutError:
        os.unlink(tmp_path)
        await update.message.reply_text(f"⏱ Timed out after {CLAUDE_TIMEOUT}s.")
        return

    response = result_text or "Claude returned an empty response."

    if new_session_id:
        if project:
            project_sessions[f"{chat_id}_{project}"] = new_session_id
            _save_project_sessions()
        else:
            sessions[chat_id] = new_session_id
            _save_sessions()
    for chunk in split_message(response):
        await update.message.reply_text(chunk)

    # Ask whether to save — don't auto-save
    original_name = doc.file_name or "document"
    save_id = str(uuid.uuid4())
    pending_saves[save_id] = {"tmp_path": tmp_path, "response": response, "original_name": original_name}
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💾 Save", callback_data=f"save_{save_id}"),
        InlineKeyboardButton("🗑 Discard", callback_data=f"discard_{save_id}"),
    ]])
    await update.message.reply_text("Save this document to captures?", reply_markup=keyboard)


async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Explicit web search via Perplexity."""
    if not is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/search <query>`", parse_mode="Markdown")
        return
    chat_id = update.effective_chat.id
    query = " ".join(context.args)
    await update.message.reply_chat_action("typing")
    await update.message.reply_text(f"🔍 Searching: _{query}_", parse_mode="Markdown")
    results = await perplexity_search(query)
    if not results:
        await update.message.reply_text("Search failed or no results.")
        return
    # Also send to Claude for synthesis
    thread_id = update.message.message_thread_id
    prompt = f"{query}\n\n{results}"
    try:
        response = await send_to_claude(prompt, chat_id, thread_id=thread_id)
    except asyncio.TimeoutError:
        await update.message.reply_text(f"⏱ Timed out after {CLAUDE_TIMEOUT}s.")
        return
    if response is not None:
        await _send_response(update, chat_id, response)


async def cmd_use(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Switch to a named project session."""
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id

    if not context.args:
        project = active_projects.get(chat_id)
        if project:
            await update.message.reply_text(f"Active project: *{project}*", parse_mode="Markdown")
        else:
            await update.message.reply_text("No active project. Use `/use <project>` to switch.", parse_mode="Markdown")
        return

    project = context.args[0].lower()

    if project in ("off", "none", "clear"):
        active_projects.pop(chat_id, None)
        _save_active_projects()
        await update.message.reply_text("Project cleared — back to default session.")
        return

    available = _list_projects()
    if project not in available:
        await update.message.reply_text(
            f"Unknown project: `{project}`\nAvailable: {', '.join(f'`{p}`' for p in available) or 'none'}",
            parse_mode="Markdown",
        )
        return

    active_projects[chat_id] = project
    _save_active_projects()

    proj_key = f"{chat_id}_{project}"
    has_session = proj_key in project_sessions
    await update.message.reply_text(
        f"Switched to project *{project}*{'  (resuming session)' if has_session else '  (new session)'}",
        parse_mode="Markdown",
    )


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List available projects and active session info."""
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    available = _list_projects()
    active = active_projects.get(chat_id)

    lines = ["*Available projects:*"]
    for p in available:
        proj_key = f"{chat_id}_{p}"
        has_session = proj_key in project_sessions
        marker = "▶ " if p == active else "  "
        session_note = " (session active)" if has_session else ""
        lines.append(f"{marker}`{p}`{session_note}")

    if not available:
        lines.append("_No projects found in ~/resources/_")

    lines.append("")
    lines.append(f"Active: *{active or 'none (default session)'}*")
    lines.append("\nUse `/use <project>` to switch, `/use off` to clear.")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    arg = (context.args[0] if context.args else "").lower()
    if arg == "on":
        voice_enabled[chat_id] = True
        await update.message.reply_text("🔊 Voice mode ON — risposte audio attive.")
    elif arg == "off":
        voice_enabled[chat_id] = False
        await update.message.reply_text("🔇 Voice mode OFF — risposte testuali.")
    else:
        status = "ON" if voice_enabled.get(chat_id) else "OFF"
        await update.message.reply_text(f"Voice mode è {status}. Usa /voice on oppure /voice off.")


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

def _extract_image_paths(text: str) -> list[str]:
    """Find local file paths in text that point to existing image files."""
    import re
    found = []
    for m in re.finditer(r'(/(?:Users|home|tmp)/[^\s\)\]\'"]+)', text):
        p = Path(m.group(1))
        if p.suffix.lower() in _IMAGE_EXTS and p.exists():
            found.append(str(p))
    return list(dict.fromkeys(found))  # deduplicate, preserve order


async def _send_response(update: Update, chat_id: int, response: str):
    """Send response as voice or text based on chat setting."""
    import time
    now = time.time()
    # Auto-send any image paths found in the response, with cooldown dedup
    for img_path in _extract_image_paths(response):
        key = (chat_id, img_path)
        if now - _recent_sends.get(key, 0) < _SEND_COOLDOWN:
            continue  # already sent recently, skip
        _recent_sends[key] = now
        try:
            with open(img_path, "rb") as f:
                await update.message.reply_photo(photo=f)
        except Exception:
            pass

    if voice_enabled.get(chat_id):
        ogg_path = await _text_to_voice(response)
        if ogg_path:
            with open(ogg_path, "rb") as f:
                await update.message.reply_voice(voice=f)
            os.unlink(ogg_path)
            return
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


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search captures by keyword."""
    if not is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/find <query>`", parse_mode="Markdown")
        return

    query = " ".join(context.args)
    captures_dir = WIKI_DIR / "captures"
    if not captures_dir.exists():
        await update.message.reply_text("No captures saved yet.")
        return

    results = []
    for md_file in sorted(captures_dir.rglob("*.md"), reverse=True):
        content = md_file.read_text()
        if query.lower() in content.lower():
            lines = [l for l in content.split("\n") if l.strip() and not l.startswith("---") and ":" not in l[:20]]
            snippet = lines[0][:120] if lines else md_file.stem
            # Find matching image file (same stem, any image extension)
            image_path = None
            for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf"):
                candidate = md_file.with_suffix(ext)
                if candidate.exists():
                    image_path = candidate
                    break
            results.append((md_file.stem, snippet, image_path))

    if not results:
        await update.message.reply_text(f"No captures found for: _{query}_", parse_mode="Markdown")
        return

    await update.message.reply_text(
        f"🔍 *Found {len(results)} result(s) for \"{query}\"*",
        parse_mode="Markdown"
    )
    for stem, snippet, image_path in results[:5]:
        caption = f"`{stem}`\n{snippet}"
        if image_path and image_path.suffix.lower() in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            with open(image_path, "rb") as f:
                await update.message.reply_photo(photo=f, caption=caption)
        elif image_path and image_path.suffix.lower() == ".pdf":
            with open(image_path, "rb") as f:
                await update.message.reply_document(document=f, caption=caption)
        else:
            await update.message.reply_text(caption, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# Forum topic handlers
# ---------------------------------------------------------------------------
async def handle_forum_topic_created(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Auto-register a new forum topic as a project session."""
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    topic_name = update.message.forum_topic_created.name
    project = _sanitize_project_name(topic_name)

    topic_key = f"{chat_id}_{thread_id}"
    topic_map[topic_key] = project
    _save_topics()
    log.info("Forum topic registered: %s -> %s (thread %s)", topic_name, project, thread_id)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("📄 Init AGENTS.md", callback_data=f"initagents_{chat_id}_{thread_id}"),
        InlineKeyboardButton("Skip", callback_data=f"initskip_{chat_id}_{thread_id}"),
    ]])
    await update.message.reply_text(
        f"Topic *{topic_name}* registered as project `{project}`.\n"
        f"Messages here will route to a dedicated Claude session.",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def _init_project_agents(chat_id: int, thread_id: int, project: str):
    """Run Claude to create AGENTS.md for a new project."""
    prompt = (
        f"Create the directory ~/resources/{project}/ and write an AGENTS.md file "
        f"for a project called '{project}'. Keep it brief: a one-line description and "
        f"a placeholder for project-specific instructions. Just do it, no explanation."
    )
    _, new_session_id, result_text = await _run_claude(
        prompt=prompt,
        session_id=None,
        skip_permissions=True,
        max_turns=3,
    )
    if new_session_id:
        project_sessions[f"{chat_id}_{project}"] = new_session_id
        _save_project_sessions()
    text = result_text or f"Project `{project}` initialized."
    await _app.bot.send_message(
        chat_id=chat_id,
        text=text,
        message_thread_id=thread_id,
    )


async def cmd_setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Register current topic to a project name, or list mappings.

    Usage (from inside a topic): /setup <project_name>
    Usage:                        /setup list
    Usage:                        /setup remove
    """
    if not is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id

    if not context.args:
        if thread_id is not None:
            key = f"{chat_id}_{thread_id}"
            current = topic_map.get(key)
            if current:
                await update.message.reply_text(
                    f"This topic → `{current}`.\nUse `/setup <project>` to change or `/setup remove` to unmap.",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    f"No mapping yet. Use `/setup <project>` to register this topic.\nThread ID: `{thread_id}`",
                    parse_mode="Markdown",
                )
        else:
            await _cmd_setup_list(update, chat_id)
        return

    arg = context.args[0].lower()

    if arg == "list":
        await _cmd_setup_list(update, chat_id)
        return

    if arg == "remove":
        if thread_id is None:
            await update.message.reply_text("Run this from inside a forum topic.")
            return
        removed = topic_map.pop(f"{chat_id}_{thread_id}", None)
        _save_topics()
        if removed:
            await update.message.reply_text(f"Removed mapping (was `{removed}`).", parse_mode="Markdown")
        else:
            await update.message.reply_text("This topic had no mapping.")
        return

    if thread_id is None:
        await update.message.reply_text(
            "Run `/setup <project>` from inside the forum topic you want to map.",
            parse_mode="Markdown",
        )
        return

    project = _sanitize_project_name(arg)
    topic_map[f"{chat_id}_{thread_id}"] = project
    _save_topics()

    available = _list_projects()
    note = " ✓ (AGENTS.md found)" if project in available else " (no AGENTS.md yet)"
    await update.message.reply_text(
        f"Topic mapped to project `{project}`{note}.",
        parse_mode="Markdown",
    )


async def _cmd_setup_list(update: Update, chat_id: int):
    """Show all topic→project mappings for this chat."""
    prefix = f"{chat_id}_"
    mappings = {k: v for k, v in topic_map.items() if k.startswith(prefix)}
    if not mappings:
        await update.message.reply_text(
            "No topic mappings yet.\nGo to a forum topic and run `/setup <project>`.",
            parse_mode="Markdown",
        )
        return
    lines = ["*Registered topics:*"]
    for key, project in sorted(mappings.items()):
        tid = key.split("_", 1)[1]
        has_session = f"{chat_id}_{project}" in project_sessions
        note = " (session active)" if has_session else ""
        lines.append(f"  Thread `{tid}` → `{project}`{note}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_plan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/plan <message> — show what Claude would do, then offer Execute/Cancel."""
    if not is_allowed(update.effective_user.id):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/plan <your request>`", parse_mode="Markdown")
        return

    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    text = " ".join(context.args)

    project = _resolve_project(chat_id, thread_id)
    if project:
        session_id = project_sessions.get(f"{chat_id}_{project}")
    else:
        session_id = sessions.get(chat_id)

    context_prefix = ""
    if project:
        ctx = _get_project_context(project)
        if ctx:
            context_prefix = f"[Project context: {project}]\n\n{ctx}\n\n---\n\n"

    plan_prompt = (
        _PROMPT_PREFIX + context_prefix +
        "[PLAN ONLY — do not use any tools. Describe step by step what you would do "
        "to complete the following request, but do not execute anything yet.]\n\n" + text
    )
    full_prompt = _PROMPT_PREFIX + context_prefix + text

    await update.message.reply_chat_action("typing")
    try:
        _, _, plan_text = await _run_claude(
            prompt=plan_prompt,
            session_id=session_id,
            skip_permissions=False,
            max_turns=3,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(f"⏱ Timed out.")
        return

    plan_text = plan_text or "Claude could not produce a plan."

    # Store the real prompt for execution
    request_id = str(uuid.uuid4())[:8]
    pending_approvals[request_id] = {
        "chat_id": chat_id,
        "thread_id": thread_id,
        "prompt": full_prompt,
        "session_id_before": session_id,
    }

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("▶️ Execute", callback_data=f"approve_{request_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"reject_{request_id}"),
        ]
    ])

    for chunk in split_message(f"📋 *Plan:*\n\n{plan_text}"):
        await update.message.reply_text(chunk, parse_mode="Markdown")
    await update.message.reply_text("Execute this plan?", reply_markup=keyboard)


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
    _app.add_handler(CommandHandler("search", cmd_search))
    _app.add_handler(CommandHandler("voice", cmd_voice))
    _app.add_handler(CommandHandler("use", cmd_use))
    _app.add_handler(CommandHandler("sessions", cmd_sessions))
    _app.add_handler(CommandHandler("find", cmd_find))
    _app.add_handler(CommandHandler("setup", cmd_setup))
    _app.add_handler(CommandHandler("plan", cmd_plan))
    _app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    _app.add_handler(MessageHandler(filters.PHOTO, handle_image))
    _app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    _app.add_handler(MessageHandler(filters.StatusUpdate.FORUM_TOPIC_CREATED, handle_forum_topic_created))
    _app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bridge started. Listening for Telegram messages...")
    _app.run_polling()


if __name__ == "__main__":
    main()
