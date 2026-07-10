from pathlib import Path
from openai import OpenAI
from config import CONFIG

client = OpenAI(base_url=CONFIG["base_url"], api_key=CONFIG["api_key"])

MAX_CHARS = CONFIG["context_max_chars"]
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def load_system_prompt() -> str:
    path = Path("system_prompt.txt")
    if not path.exists():
        return "You are a helpful AI assistant. Respond in the language the user writes in."
    return path.read_text(encoding="utf-8").strip()


SYSTEM_PROMPT = load_system_prompt()


def build_user_message(text: str, attachments: list) -> dict:
    images = [a for a in attachments if a["type"] == "image"]
    texts  = [a for a in attachments if a["type"] == "text"]
    errors = [a for a in attachments if a["type"] == "error"]

    full_text = text

    for img in images:
        full_text += f"\n\n[attached image: {img['name']} — use analyze_image tool to examine it]"

    for t in texts:
        full_text += f"\n\n[file: {t['name']}]\n{t['content']}"
    if texts:
        paths = ", ".join(f"'/workspace/{t['name']}'" for t in texts)
        full_text += f"\n\n(Files available in Python sandbox at exact paths: {paths})"

    for e in errors:
        full_text += f"\n\n[error loading {e['name']}]: {e['content']}"

    return {"role": "user", "content": full_text}


def _reconstruct_file_content(path: str, name: str) -> str:
    from files import process_file
    try:
        att = process_file(path, name)
        if att.get("type") == "text":
            return f"[file: {name}]\n{att['content']}"
        if att.get("type") == "image":
            return f"[attached image: {name} — use analyze_image tool to examine it]"
    except Exception:
        pass
    return ""


def add_file_context(history: list) -> list:
    result = []
    for row in history:
        if row["role"] != "user":
            result.append({"role": row["role"], "content": row["content"]})
            continue

        file_paths = row.get("metadata", {}).get("file_paths", [])
        additions = []
        sandbox_paths = []

        for fpath in file_paths:
            p = Path(fpath)
            if not p.exists():
                continue
            parts = p.name.split("_", 1)
            name = parts[1] if len(parts) > 1 else p.name
            chunk = _reconstruct_file_content(str(p), name)
            if chunk:
                additions.append(chunk)
            if p.suffix.lower() not in IMAGE_EXTS:
                sandbox_paths.append(f"'/workspace/{name}'")

        content = row["content"]
        if additions:
            content += "\n\n" + "\n\n".join(additions)
        if sandbox_paths:
            content += f"\n\n(Files available in Python sandbox at exact paths: {', '.join(sandbox_paths)})"

        result.append({"role": row["role"], "content": content})
    return result


def maybe_summarize(messages: list) -> list:
    total_chars = sum(
        len(m["content"]) if isinstance(m["content"], str) else 0
        for m in messages
    )

    if total_chars < MAX_CHARS:
        return messages

    recent_n = CONFIG["summary_recent"]
    to_summarize = messages[:-recent_n]
    recent = messages[-recent_n:]

    try:
        summary_resp = client.chat.completions.create(
            model=CONFIG["model"],
            messages=[
                {"role": "system", "content": "Summarize this conversation concisely. Preserve all key facts, decisions, and details."},
                {"role": "user", "content": "\n".join(
                    f"{m['role']}: {m['content']}"
                    for m in to_summarize
                )}
            ],
            max_tokens=500
        )
        summary = summary_resp.choices[0].message.content
        return [{"role": "system", "content": f"[Summary of earlier conversation]\n{summary}"}] + recent
    except Exception:
        return messages[-20:]


def build_context(history: list, new_message: dict) -> list:
    with_files = add_file_context(history)
    compressed = maybe_summarize(with_files)
    return [{"role": "system", "content": SYSTEM_PROMPT}] + compressed + [new_message]
