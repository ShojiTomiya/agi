import os
import re
import base64
from openai import OpenAI
from tavily import TavilyClient
from config import CONFIG
import files
import storage

_tavily = TavilyClient(api_key=os.getenv("TAVILY_API_KEY"))
_client = OpenAI(base_url=CONFIG["base_url"], api_key=CONFIG["api_key"])

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the web. Use when you need current information, recent events, "
                "facts you're not confident about, or anything that may have changed "
                "after your training cutoff. Returns a digest of the facts the results contain, "
                "with numbered sources. You get ONE search per question — further calls are refused, "
                "and there is no tool to open a page — so write a specific query and answer from it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "execute_python",
            "description": (
                "Execute Python code in a secure sandbox and return the output. Variables persist between calls. "
                "Use for calculations, data analysis, string processing, generating charts, or creating any files. "
                "The working directory is /workspace and holds every file in this conversation — files the user "
                "attached and files saved by earlier calls — under their own names. "
                "Available libraries: pandas, matplotlib, numpy, scipy, seaborn, openpyxl, pillow, "
                "scikit-learn, requests, reportlab, fpdf2, python-pptx, python-docx, sympy, "
                "statsmodels, opencv-python (cv2). Do NOT use any other libraries. "
                "Save output files in /workspace (a plain filename is enough); they are offered to the user "
                "for download and stay available to later calls. "
                "For charts use plt.show() — each one is saved as chart_N.png and shown to the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"}
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a document in this conversation — attached by the user or saved by execute_python: "
                "PDF (by pages), PowerPoint (slides), Jupyter notebook (cells), Word, text, code or CSV (lines). "
                "An attached document shows only its beginning in the message: use this to read the rest, "
                "and never answer about parts you haven't read. "
                "With `query` (one to three key words) it returns the passages that mention them, with their "
                "page or line numbers — the quick way to find something in a long document. Without it, it reads "
                "`count` units from `start`. At most ~6000 characters per call. "
                "For computing on tables (CSV, Excel) use execute_python instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The file's name, e.g. 'umowa.pdf'"},
                    "start": {"type": "integer", "description": "First page/slide/cell/line to read, counting from 1"},
                    "count": {"type": "integer", "description": "How many to read (default: 3 pages, 10 slides or cells, 200 lines)"},
                    "query": {"type": "string", "description": "Key words to look for, instead of reading a range"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": (
                "Look at an image file in this conversation (png, jpg, jpeg, webp, gif) with a vision model — "
                "one the user attached, or one saved by execute_python such as 'chart_2.png'. "
                "ONLY for image files — never for CSV, PDF, Excel, or text files. "
                "Ask a specific question to get the most useful response."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "The image's file name, e.g. 'photo.jpg' or 'chart_2.png'"},
                    "question": {"type": "string", "description": "What to ask about the image"}
                },
                "required": ["filename", "question"]
            }
        }
    }
]



# raw results passed to the main model only if the digest can't be made
MAX_SEARCH_CHARS = 3000
# keeps the digest request under the helper model's 8000-token request limit
MAX_DIGEST_INPUT_CHARS = 18000

# The helper only extracts; deciding whether the facts answer the question is left to the main
# model. Asked to judge too, the small model sometimes dropped relevant facts that didn't match
# the question exactly, and the main model then filled the gap from memory.
DIGEST_PROMPT = (
    "You extract facts from web search results for another assistant, who writes the answer. "
    "List every fact in the results that is relevant to the user's question — names, numbers, dates "
    "and lists copied exactly as written — each marked with its source number like [2]. "
    "Copy, don't interpret: no guesses, nothing that isn't in the text, and don't judge whether the "
    "facts fully answer the question. If nothing relevant is there, say so. "
    "At most 200 words. End with 'Sources:' and the numbered URLs you cited."
)


def _digest_results(question: str, results: str) -> str:
    try:
        resp = _client.chat.completions.create(
            model=CONFIG["helper_model"],
            messages=[
                {"role": "system", "content": DIGEST_PROMPT},
                {"role": "user", "content": f"Question: {question}\n\nSearch results:\n{results[:MAX_DIGEST_INPUT_CHARS]}"},
            ],
            # headroom so reasoning can't use up the whole budget and leave an empty digest
            max_tokens=1500,
            extra_body=CONFIG.get("helper_extra_body"),
        )
    except Exception:
        return ""
    return (resp.choices[0].message.content or "").strip()


def search_web(query: str, question: str = "", max_results: int = 5) -> dict:
    """Search with Tavily, then let the helper model condense the full results.

    Handing the main model raw results meant either cutting them blindly (it then
    searched again for what was cut) or spending its rate limit on reading them.
    """
    try:
        resp = _tavily.search(query=query, max_results=max_results, search_depth="advanced")
    except Exception as e:
        return {"content": f"Search failed: {e}"}
    results = resp.get("results", [])
    if not results:
        return {"content": "No results found."}

    raw = "\n\n".join(f"[{i}] {r['title']} ({r['url']})\n{r['content']}" for i, r in enumerate(results, 1))
    digest = _digest_results((question or query)[:500], raw)
    if digest:
        return {"content": digest, "detail": raw}

    text = raw if len(raw) <= MAX_SEARCH_CHARS else (
        raw[:MAX_SEARCH_CHARS] + "\n\n[results truncated — answer with what's above rather than searching again]"
    )
    return {"content": text, "detail": raw}


# Said right where the code failed: after two failed runs gpt-oss used to answer with a
# made-up description of the chart and a link to a file that didn't exist. Retrying
# comes first — told "fix it or say so", it gave up straight away, and in English.
CODE_FAILED_NOTE = (
    "[The code stopped at this error. Fix it and run it again — a simpler approach is fine. "
    "Only if you can't get it to work, tell the user so in their language; never describe "
    "results, charts or files that weren't produced.]"
)


def execute_python(code: str, session_id: str = None) -> dict:
    """Run code in the session's sandbox. Returns the text output plus the names of
    images and other files the code created or changed in /workspace."""
    import kernel

    if not session_id:
        return {'content': 'Error: no session_id for kernel', 'charts': [], 'files': []}

    result = kernel.execute(session_id, code)
    images = result.get('images', [])
    files = result.get('files', [])

    lines = []
    text_out = (result.get('stdout') or '').strip()
    if result.get('error'):
        text_out = (text_out + '\nError:\n' + result['error']).strip() + '\n' + CODE_FAILED_NOTE
    if text_out:
        lines.append(text_out)
    for name in images:
        # the main model can't see images: it used to describe the chart it meant to draw
        # ("clearly spread out") while the plot showed the points bunched in one corner
        lines.append(f"[image saved: '{name}' — shown to the user. You can't see it: before saying anything "
                     f"about what it shows, look at it with analyze_image('{name}', ...)]")
    for name in files:
        lines.append(f"[file saved: '{name}' — offered to the user for download]")

    return {'content': '\n'.join(lines) or '(no output)', 'charts': images, 'files': files}


def _session_file(filename: str, session_id: str):
    """(name, path) of a file in the session; path is None if there's no such file.

    Files live in the session directory under their own names; the sandbox's
    /workspace/ prefix is accepted since that's how execute_python sees them.
    """
    name = filename.strip().removeprefix("/workspace/").lstrip("/")
    try:
        path = storage.session_path(session_id, name)
    except ValueError:
        return name, None
    return name, path if path.is_file() else None


MAX_READ_CHARS = 6000
DEFAULT_READ_COUNT = {"page": 3, "slide": 10, "cell": 10, "line": 200}
MAX_MATCHES = 6
LINES_PER_SECTION = 20


def _stems(query: str) -> list[str]:
    """Lower-cased word stems, so inflected forms match: "wypowiedzenie" finds
    "wypowiedzenia", "termination" finds "terminated". Crude, but documents here
    are mostly Polish, where exact-word search misses most matches."""
    stems = []
    for word in re.findall(r"\w+", query.casefold()):
        if len(word) >= 8:
            word = word[:-3]
        elif len(word) > 4:
            word = word[:max(4, len(word) - 2)]
        if len(word) > 1:
            stems.append(word)
    return stems


def read_file(filename: str, start: int = 1, count: int = None, query: str = "", session_id: str = None) -> str:
    name, path = _session_file(filename, session_id)
    if path is None:
        return f"File '{name}' not found."
    if storage.is_image(name):
        return f"'{name}' is an image — use analyze_image to look at it."
    try:
        unit, items = files.read_units(str(path), name)
    except Exception as e:
        return f"Can't read '{name}': {e}"
    if query:
        return _search_file(name, unit, items, query)

    try:
        start = max(1, int(start or 1))
        count = max(1, int(count or DEFAULT_READ_COUNT[unit]))
    except (TypeError, ValueError):
        return "start and count must be whole numbers."
    total = len(items)
    if start > total:
        return f"'{name}' has only {total} {unit}s."
    end = min(total, start + count - 1)

    blocks, used, shown_to = [], 0, start - 1
    for i in range(start, end + 1):
        block = files.render_unit(unit, i, items[i - 1])
        if blocks and used + len(block) > MAX_READ_CHARS:
            break
        if len(block) > MAX_READ_CHARS:
            block = block[:MAX_READ_CHARS] + f"…[{unit} cut at {MAX_READ_CHARS} characters]"
        blocks.append(block)
        used += len(block)
        shown_to = i

    more = f"[continue with read_file('{name}', start={shown_to + 1})]" if shown_to < total else "[end of file]"
    return f"{name} — {unit}s {start}–{shown_to} of {total}\n\n{files.join_units(unit, blocks)}\n\n{more}"


def _search_file(name: str, unit: str, items: tuple, query: str) -> str:
    stems = _stems(query)
    if not stems:
        return "Give the query a word or two to look for."

    if unit == "line":
        sections = [(f"lines {i + 1}–{min(i + LINES_PER_SECTION, len(items))}", "\n".join(items[i:i + LINES_PER_SECTION]))
                    for i in range(0, len(items), LINES_PER_SECTION)]
    else:
        sections = [(f"{unit} {i}", text) for i, text in enumerate(items, 1)]

    matches = []
    for label, text in sections:
        low = text.casefold()
        found = [s for s in stems if s in low]
        if found:
            matches.append((len(set(found)), label, text, min(low.find(s) for s in found)))
    if not matches:
        return f"No passages in '{name}' mention {query!r}."
    # most query words first; among equals, document order
    matches.sort(key=lambda m: -m[0])

    parts, used = [], 0
    for _, label, text, pos in matches[:MAX_MATCHES]:
        a, b = max(0, pos - 300), min(len(text), pos + 500)
        snippet = ("…" if a else "") + text[a:b].strip() + ("…" if b < len(text) else "")
        block = f"[{label}] {snippet}"
        if parts and used + len(block) > MAX_READ_CHARS:
            break
        parts.append(block)
        used += len(block)

    where = "sections" if unit == "line" else f"{unit}s"
    return (f"{name} — {len(matches)} {where} mention {query!r}; showing {len(parts)}:\n\n" + "\n\n".join(parts)
            + f"\n\n[read a whole {unit if unit != 'line' else 'range'} with read_file('{name}', start=…)]")


# A chart of DPP points bunched in the top rows came back as "a sample from a DPP showing
# repulsion": the vision model read the title, and the main model trusted what the code
# was meant to draw over what the vision model saw.
VISION_PROMPT = (
    "Describe only what is actually visible: positions, clusters, gaps, counts, trends, text. "
    "Don't infer from a title or labels what the image is supposed to show. A few sentences."
)
SEEN_NOTE = (
    "[This is what the image actually shows. Describe it from this, not from what the code was "
    "meant to produce — if it isn't what you expected, the code is probably wrong: say so plainly "
    "(or fix it). Answer the user in their language.]"
)


def analyze_image(filename: str, question: str, session_id: str = None) -> str:
    name, image_path = _session_file(filename, session_id)
    if not storage.is_image(name):
        return f"'{name}' is not an image file. analyze_image only works with png/jpg/jpeg/webp/gif. Use read_file or execute_python for this file."
    if image_path is None:
        return f"Image '{name}' not found."

    ext = name.rsplit(".", 1)[-1].lower()
    media_type = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    b64 = base64.b64encode(image_path.read_bytes()).decode()

    try:
        resp = _client.chat.completions.create(
            model=CONFIG["vision_model"],
            messages=[{"role": "system", "content": VISION_PROMPT}, {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                    {"type": "text", "text": question},
                ]
            }],
            max_tokens=1024,
            extra_body=CONFIG.get("vision_extra_body"),
        )
    except Exception as e:
        return f"Vision analysis failed: {e}"
    # some vision models inline their reasoning; it only costs the main model tokens
    content = re.sub(r"<think>.*?(</think>|$)", "", resp.choices[0].message.content or "", flags=re.S).strip()
    if not content:
        return "The vision model returned no description."
    return f"{content}\n{SEEN_NOTE}"


def execute_tool(name: str, args: dict, session_id: str = None, question: str = "") -> dict | str:
    if name == "search_web":
        return search_web(args.get("query", ""), question=question)
    if name == "execute_python":
        return execute_python(args.get("code", ""), session_id=session_id)
    if name == "read_file":
        return read_file(args.get("name", ""), args.get("start", 1), args.get("count"), args.get("query", ""),
                         session_id=session_id)
    if name == "analyze_image":
        return analyze_image(args.get("filename", ""), args.get("question", "Describe this image."), session_id=session_id)
    return f"Unknown tool: {name}"
