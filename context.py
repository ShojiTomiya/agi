import hashlib
import json
from pathlib import Path
from openai import OpenAI
from config import CONFIG
import storage

client = OpenAI(base_url=CONFIG["base_url"], api_key=CONFIG["api_key"])

MAX_CHARS = CONFIG["context_max_chars"]


def load_system_prompt() -> str:
    path = Path("system_prompt.txt")
    if not path.exists():
        return "You are a helpful AI assistant. Respond in the language the user writes in."
    return path.read_text(encoding="utf-8").strip()


SYSTEM_PROMPT = load_system_prompt()


def build_user_message(text: str, attachments: list) -> dict:
    """The user's text plus a short description of each file attached to it
    (a short document whole, a long one only its beginning — see files.py)."""
    full_text = text
    for a in attachments:
        if a["type"] == "image":
            full_text += f"\n\n[attached image: {a['name']} — look at it with analyze_image]"
        elif a["type"] == "text":
            full_text += f"\n\n[attached file: {a['name']}]\n{a['content']}"
        else:
            full_text += f"\n\n[couldn't load {a['name']}]: {a['content']}"
    if any(a["type"] != "error" for a in attachments):
        full_text += "\n\n(Attached files are in the Python sandbox's working directory, /workspace.)"
    return {"role": "user", "content": full_text}


def _earlier_attachment(name: str) -> str:
    if storage.is_image(name):
        return f"[attached earlier: {name} — image; look at it with analyze_image]"
    return f"[attached earlier: {name} — in /workspace; read it with read_file or execute_python]"


def add_file_context(history: list) -> list:
    """History as model messages. A file attached in an earlier turn becomes a one-line
    reference: its description was in that turn, and tools can read it again — pasting
    it into every later request only spent the rate limit."""
    result = []
    for row in history:
        # a failed turn is saved for the UI; the error text means nothing to the model
        if row["role"] == "assistant" and row.get("metadata", {}).get("error"):
            continue
        content = row["content"]
        if row["role"] == "user":
            names = row.get("metadata", {}).get("files", [])
            if names:
                content += "\n\n" + "\n".join(_earlier_attachment(n) for n in names)
        result.append({"role": row["role"], "content": content})
    return result


# session_id -> (number of messages covered, digest of those messages, summary text)
_summaries: dict = {}


def _chars(messages: list) -> int:
    return sum(len(m["content"]) for m in messages if isinstance(m["content"], str))


def _digest(messages: list) -> str:
    return hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()


def _with_summary(summary: str, messages: list) -> list:
    if not summary:
        return messages
    return [{"role": "system", "content": f"[Summary of earlier conversation]\n{summary}"}] + messages


def _summarize(previous: str, messages: list) -> str:
    transcript = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
    if previous:
        transcript = f"[Earlier summary]\n{previous}\n\n[New messages]\n{transcript}"
    resp = client.chat.completions.create(
        model=CONFIG["helper_model"],
        messages=[
            {"role": "system", "content": "Summarize this conversation concisely. Preserve all key facts, decisions, and details."},
            {"role": "user", "content": transcript},
        ],
        max_tokens=1024,
        extra_body=CONFIG.get("helper_extra_body"),
    )
    return (resp.choices[0].message.content or "").strip()


def maybe_summarize(messages: list, session_id: str = None) -> list:
    # Reuse the summary from an earlier turn while it still matches the start of
    # the history (an edited message invalidates it), so the model is only asked
    # to summarize again once the unsummarized tail grows past the limit.
    covered, summary = 0, ""
    cached = _summaries.get(session_id)
    if cached:
        n, digest, text = cached
        if n <= len(messages) and _digest(messages[:n]) == digest:
            covered, summary = n, text

    current = _with_summary(summary, messages[covered:])
    if _chars(current) < MAX_CHARS:
        return current

    fold_to = max(covered, len(messages) - CONFIG["summary_recent"])
    if fold_to == covered:
        return current

    try:
        new_summary = _summarize(summary, messages[covered:fold_to])
    except Exception:
        new_summary = ""
    if not new_summary:
        # better to lose old turns than to send a request the provider will reject
        return messages[fold_to:]

    if session_id:
        _summaries[session_id] = (fold_to, _digest(messages[:fold_to]), new_summary)
    return _with_summary(new_summary, messages[fold_to:])


MAX_TOOL_RESULT_CHARS = 1500
MAX_PREVIOUS_TOOLS_CHARS = 3000


def previous_tools_note(steps: list) -> dict | None:
    """The tool calls behind the previous answer, so a follow-up question can build on
    them instead of searching or computing again. Only the answer text is in history."""
    lines, call = [], None
    for step in steps:
        if step.get("step") == "tool_call":
            call = step
        elif step.get("step") == "tool_result" and call:
            args = json.dumps(call.get("args", {}), ensure_ascii=False)[:300]
            result = (step.get("result") or "")[:MAX_TOOL_RESULT_CHARS]
            lines.append(f"- {step.get('name')}({args}) →\n{result}")
            call = None
    if not lines:
        return None
    text = "\n".join(lines)[:MAX_PREVIOUS_TOOLS_CHARS]
    return {"role": "system", "content": f"[Tool calls behind your previous answer]\n{text}"}


def build_context(history: list, new_message: dict, session_id: str = None, previous_steps: list = None) -> list:
    with_files = add_file_context(history)
    compressed = maybe_summarize(with_files, session_id)
    note = previous_tools_note(previous_steps or [])
    return [{"role": "system", "content": SYSTEM_PROMPT}] + compressed + ([note] if note else []) + [new_message]
