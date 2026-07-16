import json
import re
from pathlib import Path
from types import SimpleNamespace
from openai import BadRequestError, RateLimitError
from context import build_user_message, build_context, client
from config import CONFIG
from files import process_file
from tools import TOOLS, execute_tool

MAX_ITERATIONS = 6
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def _fmt(msg):
    content = msg.get("content") if isinstance(msg, dict) else msg
    tool_calls = msg.get("tool_calls") if isinstance(msg, dict) else None

    if tool_calls:
        tc_parts = []
        for tc in tool_calls:
            name = tc["function"]["name"]
            args = tc["function"]["arguments"]
            tc_parts.append(f"[tool_call: {name}]\n{args}")
        tc_str = "\n".join(tc_parts)
        if content:
            return f"{content}\n\n{tc_str}" if isinstance(content, str) else tc_str
        return tc_str

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for b in content:
        if b.get("type") == "image_url":
            parts.append("[Image]")
        elif b.get("type") == "text":
            parts.append(b["text"])
    return "\n".join(parts)


def run_agent(message: str, file_paths: list, filenames: list, history: list, session_id: str = None):
    yield {"type": "step", "step": "user_msg", "text": message, "filenames": filenames}

    attachments = []
    for path, name in zip(file_paths, filenames):
        att = process_file(path, name)
        attachments.append(att)
        if att["type"] == "image":
            yield {"type": "step", "step": "file_read", "filename": name, "kind": "image"}
        elif att["type"] == "text":
            yield {"type": "step", "step": "file_read", "filename": name, "kind": "text", "chars": len(att["content"])}
        elif att["type"] == "error":
            yield {"type": "step", "step": "file_read", "filename": name, "kind": "error", "error": att["content"]}

    new_message = build_user_message(message, attachments)
    messages = build_context(history, new_message)

    # collect all files (current + history) for tool access
    sandbox_files = list(attachments)
    seen_names = {att["name"] for att in attachments}
    for row in history:
        for fpath in row.get("metadata", {}).get("file_paths", []):
            p = Path(fpath["path"] if isinstance(fpath, dict) else fpath)
            if not p.exists():
                continue
            parts = p.name.split("_", 1)
            name = parts[1] if len(parts) > 1 else p.name
            if name not in seen_names:
                seen_names.add(name)
                ext = p.suffix.lower()
                file_type = "image" if ext.lstrip(".") in {e.lstrip(".") for e in IMAGE_EXTS} else "text"
                sandbox_files.append({"type": file_type, "name": name, "original_path": str(p)})

    all_chart_paths = []
    all_file_paths = []
    search_web_calls = 0
    model = CONFIG["model"]

    try:
        for _ in range(MAX_ITERATIONS):
            yield {
                "type": "step", "step": "llm_call", "model": model,
                "messages": [{"role": m["role"], "content": _fmt(m)} for m in messages],
            }

            answer = ""
            reasoning = ""
            tc_map = {}
            usage = None
            groq_failed = None

            try:
                stream = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    max_tokens=CONFIG["max_tokens"],
                    stream=True,
                    stream_options={"include_usage": True},
                )
                for chunk in stream:
                    if not chunk.choices:
                        if chunk.usage:
                            usage = chunk.usage
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta

                    if delta.content:
                        answer += delta.content
                        yield {"type": "token", "token": delta.content}

                    reasoning_delta = getattr(delta, "reasoning", None)
                    if reasoning_delta:
                        if not reasoning:
                            yield {"type": "step", "step": "reasoning_start"}
                        reasoning += reasoning_delta

                    if delta.tool_calls:
                        for tc_chunk in delta.tool_calls:
                            idx = tc_chunk.index
                            if idx not in tc_map:
                                tc_map[idx] = {"id": "", "name": "", "arguments": ""}
                            if tc_chunk.id:
                                tc_map[idx]["id"] = tc_chunk.id
                            if tc_chunk.function:
                                if tc_chunk.function.name:
                                    tc_map[idx]["name"] += tc_chunk.function.name
                                if tc_chunk.function.arguments:
                                    tc_map[idx]["arguments"] += tc_chunk.function.arguments

                    if chunk.usage:
                        usage = chunk.usage

            except BadRequestError as e:
                body = getattr(e.response, "json", lambda: {})()
                error_body = body.get("error", {})
                failed = error_body.get("failed_generation", "")
                if failed:
                    yield {"type": "token", "token": failed}
                    yield {"type": "done", "answer": failed, "usage": None, "model": model, "chart_paths": all_chart_paths, "file_paths": all_file_paths}
                    return
                if "tool call validation failed" in error_body.get("message", "").lower():
                    messages.append({
                        "role": "user",
                        "content": (
                            "(system: your last tool call was invalid — either the tool doesn't exist or its "
                            "arguments were malformed/incomplete. You only have search_web (requires 'query'), "
                            "execute_python, generate_image, analyze_image — there is no browser/file-opening tool. "
                            "If you already have search results, answer directly instead of searching again.)"
                        ),
                    })
                    continue
                raise

            if reasoning:
                yield {"type": "step", "step": "reasoning", "text": reasoning}

            tool_calls = [
                SimpleNamespace(
                    id=tc_map[i]["id"],
                    function=SimpleNamespace(
                        name=tc_map[i]["name"],
                        arguments=tc_map[i]["arguments"]
                    )
                )
                for i in sorted(tc_map.keys())
            ]

            if not tool_calls:
                usage_dict = {
                    "prompt_tokens":     usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens":      usage.total_tokens,
                } if usage else None
                yield {"type": "step", "step": "response", "content": answer, "usage": usage_dict}
                yield {"type": "done", "answer": answer, "usage": usage_dict, "model": model, "chart_paths": all_chart_paths, "file_paths": all_file_paths}
                return

            messages.append({
                "role": "assistant",
                "content": answer or None,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                func_args = json.loads(tc.function.arguments)

                yield {"type": "step", "step": "tool_call", "name": tc.function.name, "args": func_args}

                if tc.function.name == "search_web":
                    search_web_calls += 1

                if tc.function.name == "search_web" and search_web_calls > 1:
                    raw = "You already searched the web this turn. Do not search again — answer the user's question now using the results you already have."
                else:
                    raw = execute_tool(tc.function.name, func_args, attachments=sandbox_files, session_id=session_id)

                # Normalise to dict
                if isinstance(raw, dict):
                    tool_content = raw.get('content', '(no output)')
                    all_chart_paths.extend(raw.get('chart_paths', []))
                    all_file_paths.extend(raw.get('file_paths', []))
                    iter_files = raw.get('file_paths', [])
                else:
                    # string result (search_web, analyze_image, etc.)
                    tool_content = raw
                    iter_files = []

                # Inspector display value (always text)
                if isinstance(tool_content, list):
                    display = " ".join(
                        p.get('text', '[image]') if p['type'] == 'text' else '[image]'
                        for p in tool_content
                    )
                else:
                    display = tool_content

                yield {"type": "step", "step": "tool_result", "name": tc.function.name, "result": display}

                if iter_files:
                    names = ", ".join(f["name"] for f in iter_files)
                    note = f"\nFile(s) generated and available for download: {names}"
                    if isinstance(tool_content, list):
                        tool_content.append({'type': 'text', 'text': note})
                    else:
                        tool_content = (tool_content or '') + note

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_content or "(no output)",
                })

        yield {"type": "error", "message": "Agent exceeded maximum iterations."}

    except BadRequestError as e:
        body = getattr(e.response, "json", lambda: {})()
        failed = body.get("error", {}).get("failed_generation", "")
        if failed:
            yield {"type": "token", "token": failed}
            yield {"type": "done", "answer": failed, "usage": None, "model": model, "chart_paths": all_chart_paths, "file_paths": all_file_paths}
        else:
            yield {"type": "error", "message": f"Unexpected error: {str(e)}"}
    except RateLimitError as e:
        body = getattr(e.response, "json", lambda: {})()
        message = body.get("error", {}).get("message", str(e))
        yield {"type": "error", "message": f"Rate limit: {message}"}
    except Exception as e:
        yield {"type": "error", "message": f"Unexpected error: {str(e)}"}
