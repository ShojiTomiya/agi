import json
import re
import time
from types import SimpleNamespace
from openai import (
    OpenAI, APIConnectionError, APIError, APIStatusError, InternalServerError, RateLimitError,
)
from context import build_user_message, build_context
from config import CONFIG
from files import process_file
from tools import TOOLS, execute_tool
import storage

MAX_ITERATIONS = 6

RETRIES = 3
# longer waits (e.g. the daily token limit) are reported instead of waited out
MAX_RATE_LIMIT_WAIT = 60

# No SDK retries: the loop waits out rate limits itself so the UI can say it's waiting
client = OpenAI(base_url=CONFIG["base_url"], api_key=CONFIG["api_key"], max_retries=0)


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


def _parse_args(raw: str):
    """Return (args, error) for a tool call's JSON arguments string."""
    try:
        args = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return {}, str(e)
    if not isinstance(args, dict):
        return {}, "arguments must be a JSON object"
    return args, None


def _retry_after(e: RateLimitError) -> float | None:
    """Seconds the provider asks us to wait, from the header or the error message."""
    value = (getattr(e.response, "headers", None) or {}).get("retry-after")
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    m = re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)(ms|s)", str(e))
    if not m:
        return None
    seconds = float(m.group(2)) / (1000 if m.group(3) == "ms" else 1)
    return int(m.group(1) or 0) * 60 + seconds


def _open_stream(**kwargs):
    """Start a streamed completion, waiting out short rate limits and transient errors.

    A generator: yields a step for every rate-limit wait, returns the stream.
    """
    for attempt in range(RETRIES + 1):
        try:
            return client.chat.completions.create(**kwargs)
        except RateLimitError as e:
            wait = _retry_after(e)
            if wait is None or wait > MAX_RATE_LIMIT_WAIT or attempt == RETRIES:
                raise
            yield {"type": "step", "step": "rate_limit_wait", "seconds": round(wait, 1)}
            time.sleep(wait + 0.5)
        except (APIConnectionError, InternalServerError):
            if attempt == RETRIES:
                raise
            time.sleep(1 + attempt)


def run_agent(message: str, files: list, history: list,
              session_id: str = None, previous_steps: list = None):
    """`files` are names of files the user attached, already in the session directory."""
    yield {"type": "step", "step": "user_msg", "text": message, "filenames": files}

    attachments = []
    for name in files:
        att = process_file(str(storage.session_path(session_id, name)), name)
        attachments.append(att)
        if att["type"] == "image":
            yield {"type": "step", "step": "file_read", "filename": name, "kind": "image"}
        elif att["type"] == "text":
            yield {"type": "step", "step": "file_read", "filename": name, "kind": "text", "chars": len(att["content"])}
        elif att["type"] == "error":
            yield {"type": "step", "step": "file_read", "filename": name, "kind": "error", "error": att["content"]}

    new_message = build_user_message(message, attachments)
    messages = build_context(history, new_message, session_id, previous_steps)

    # names of images / other files the model's code produced this turn
    all_charts = []
    all_files = []
    search_web_calls = 0
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    model = CONFIG["model"]
    tool_names = ", ".join(t["function"]["name"] for t in TOOLS)

    try:
        for iteration in range(MAX_ITERATIONS):
            # On the last step tools are switched off, so the turn always ends in an
            # answer built from what was gathered instead of a max-iterations error.
            last = iteration == MAX_ITERATIONS - 1
            if last:
                messages.append({
                    "role": "user",
                    "content": "(system: this is your last step — tools are off. Answer now with what you have.)",
                })

            yield {
                "type": "step", "step": "llm_call", "model": model,
                "messages": [{"role": m["role"], "content": _fmt(m)} for m in messages],
            }

            answer = ""
            reasoning = ""
            tc_map = {}
            usage = None
            started = time.time()

            try:
                stream = yield from _open_stream(
                    model=model,
                    messages=messages,
                    max_tokens=CONFIG["max_tokens"],
                    stream=True,
                    stream_options={"include_usage": True},
                    extra_body=CONFIG.get("model_extra_body"),
                    # tools are left out rather than sent with tool_choice="none":
                    # gpt-oss calls them anyway and Groq then rejects the request
                    **({} if last else {"tools": TOOLS}),
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

            except APIError as e:
                # Groq rejects a call when the model produced a broken tool call
                # (unparseable JSON, unknown tool) — give it another try with a hint.
                # It comes either as a 400 before the stream starts or as an error
                # chunk inside a stream that already began (a plain APIError then);
                # in both cases the SDK puts the "error" object in e.body.
                error_body = e.body if isinstance(e.body, dict) else {}
                malformed = (error_body.get("code") == "tool_use_failed"
                             or "tool call validation failed" in error_body.get("message", "").lower())
                if malformed and not last:
                    yield {"type": "step", "step": "retry", "reason": error_body.get("message", "")[:200]}
                    messages.append({
                        "role": "user",
                        "content": (
                            "(system: your last tool call was invalid — the tool doesn't exist or its arguments "
                            f"weren't valid JSON. You only have: {tool_names} — there is no browser/file-opening "
                            "tool. Call one of them again with valid JSON arguments, or answer directly.)"
                        ),
                    })
                    continue
                failed = error_body.get("failed_generation", "")
                if failed:
                    # out of tries: what the model produced beats no answer at all
                    yield {"type": "token", "token": failed}
                    yield {"type": "done", "answer": failed, "usage": None, "model": model,
                           "charts": all_charts, "files": all_files}
                    return
                raise

            call_usage = {k: getattr(usage, k, 0) or 0 for k in total_usage} if usage else None
            if call_usage:
                for key in total_usage:
                    total_usage[key] += call_usage[key]
            yield {"type": "step", "step": "llm_call_done", "usage": call_usage,
                   "ms": int((time.time() - started) * 1000)}

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

            if not tool_calls or last:
                if not answer:
                    answer = "I couldn't put together an answer within the step limit for one reply."
                    yield {"type": "token", "token": answer}
                # summed over every LLM call in this turn, not just the last one
                usage_dict = dict(total_usage) if total_usage["total_tokens"] else None
                yield {"type": "step", "step": "response", "content": answer, "usage": usage_dict}
                yield {"type": "done", "answer": answer, "usage": usage_dict, "model": model,
                       "charts": all_charts, "files": all_files}
                return

            messages.append({
                "role": "assistant",
                "content": answer or None,
                "tool_calls": [
                    # malformed arguments echoed back can get the next request rejected
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name,
                                  "arguments": tc.function.arguments if _parse_args(tc.function.arguments)[1] is None else "{}"}}
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                func_args, args_error = _parse_args(tc.function.arguments)

                yield {"type": "step", "step": "tool_call", "name": tc.function.name,
                       "args": func_args if not args_error else {"_raw": tc.function.arguments}}

                if tc.function.name == "search_web":
                    search_web_calls += 1

                if args_error:
                    raw = f"Error: invalid tool arguments ({args_error}). Call {tc.function.name} again with valid JSON arguments."
                elif tc.function.name == "search_web" and search_web_calls > 1:
                    raw = "You already searched the web this turn. Do not search again — answer the user's question now using the results you already have."
                else:
                    raw = execute_tool(tc.function.name, func_args, session_id=session_id, question=message)

                # tools return either text or {"content": text, plus optional charts/files/detail}
                if isinstance(raw, dict):
                    tool_content = raw.get("content") or "(no output)"
                    for name in raw.get("charts", []):
                        if name not in all_charts:
                            all_charts.append(name)
                    for name in raw.get("files", []):
                        if name not in all_files:
                            all_files.append(name)
                else:
                    tool_content = raw or "(no output)"

                step = {"type": "step", "step": "tool_result", "name": tc.function.name, "result": tool_content}
                if isinstance(raw, dict) and raw.get("detail"):
                    # what the tool saw before condensing it, for the inspector only
                    step["detail"] = raw["detail"]
                yield step

                messages.append({"role": "tool", "tool_call_id": tc.id, "content": tool_content})

    except RateLimitError as e:
        body = getattr(e.response, "json", lambda: {})()
        message = body.get("error", {}).get("message", str(e))
        yield {"type": "error", "message": f"Rate limit: {message}"}
    except APIStatusError as e:
        if e.status_code == 413:
            yield {"type": "error", "message": (
                "This request is larger than the model's per-request token limit. "
                "Start a new conversation or attach a smaller file."
            )}
        else:
            yield {"type": "error", "message": f"Unexpected error: {str(e)}"}
    except Exception as e:
        yield {"type": "error", "message": f"Unexpected error: {str(e)}"}
