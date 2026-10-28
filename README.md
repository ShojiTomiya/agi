# Local AI chat

A self-hosted AI chat app with an agentic loop, streaming responses and python execution sandbox. Built with FastAPI, SQLite, and vanilla JS.

## Features

- **Multi-provider support** - Groq, Ollama (local), Gemini
- **Streaming responses** - tokens rendered in real time with Markdown and LaTeX (KaTeX)
- **Python sandbox** - Docker kernel per session; variables survive between messages, matplotlib charts render inline
- **Web search** - via Tavily API; a small helper model condenses the full results into a short digest with sources before the main model sees them
- **Image analysis** - vision model for uploaded images and generated charts
- **File attachments** - PDF, Word, Excel, CSV, PowerPoint, Jupyter notebooks, text/code, images. A document goes into the message as a short description (a short one whole, a long one only its beginning); the model reads the rest with `read_file` - by pages, slides or lines, or by searching for key words (inflected Polish forms match too). CSV/Excel get a table preview and are analysed in the Python sandbox
- **Loop inspector** - sidebar showing each LLM call (with tokens and time), reasoning, tool call, result, rate-limit wait and retry; every turn is saved, so clicking any past message shows how it was produced
- **Resilient agent loop** - waits out short rate limits (and says so), retries malformed tool calls, and always ends a turn with an answer - the last step runs without tools; a follow-up question sees the previous turn's tool results
- **Session management** - history, rename, delete, edit messages (a failed turn is kept and can be edited)

## Setup

### 1. Clone and install

```bash
git clone https://github.com/ShojiTomiya/agi.git
cd agi
pip install -r requirements.txt
```

### 2. Configure environment

Create a `.env` file in the project root:

```env
# Provider: groq | ollama | gemini
LLM_PROVIDER=groq

# Groq (https://console.groq.com)
GROQ_API_KEY=your_key_here

# Gemini (https://aistudio.google.com)
GEMINI_API_KEY=your_key_here

# Web search (https://app.tavily.com)
TAVILY_API_KEY=your_key_here
```

For **Ollama**, no API key is needed - just make sure Ollama is running locally (`ollama serve`).

### 3. Build the sandbox image

The Python execution sandbox runs in Docker. Build the image once:

```bash
docker build -f Dockerfile.sandbox -t agi-sandbox:latest .
```

The image includes: pandas, matplotlib, numpy, scipy, seaborn, scikit-learn, reportlab, fpdf2, python-pptx, python-docx, sympy, statsmodels, opencv-python-headless, openpyxl, xlrd, tabulate, Pillow, and DejaVu fonts (for Unicode/Polish text in PDFs).

`repl_server.py` is copied into each container when it starts, so editing it needs no rebuild. Changing `Dockerfile.sandbox` does - remove the old image (`docker rmi agi-sandbox:latest`) and restart the app to rebuild it.

### 4. Run

```bash
uvicorn main:app --reload
```

Open [http://localhost:8000](http://localhost:8000).

## Provider configuration

Switch providers by setting `LLM_PROVIDER` in `.env`. Models and parameters are configured in `config.py`.

| Provider | Models | Notes |
|----------|--------|-------|
| `groq` | gpt-oss-120b (main), qwen3.8-27b (vision), gpt-oss-20b (helper: search digests, history summaries) | Free tier - tight limits: 8K tokens/min, 200K tokens/day, counted separately per model |
| `ollama` | qwen3:8b, qwen2.5vl:7b | Fully local, no API key, no rate limits |
| `gemini` | gemini-2.0-flash | Free tier via AI Studio |

## Python sandbox

Each chat session gets a dedicated Docker container with a python namespace - variables defined in one message are available in the next, like a Jupyter notebook. The model can:

- Run calculations and data analysis
- Generate charts (`plt.show()` saves each one as `chart_N.png` and shows it in chat)
- Create files (Excel, PDF, CSV, Word, PowerPoint) - available for download in chat
- Read and change any file in the conversation

Every session's files live in one directory, `uploads/<session_id>/`, which is mounted into its container as `/workspace`. Uploads, files the model saves and charts all sit there under their own names, so the app, the sandbox and the browser refer to a file by the same name, and nothing is copied in or out. Files stay when the container is restarted.

## Project structure

```
main.py            # FastAPI app, routes
agent.py           # Agentic loop with streaming
tools.py           # Tool definitions and implementations
kernel.py          # Docker sandbox lifecycle management
repl_server.py     # HTTP REPL server running inside the container
context.py         # Message history, system prompt, token management
files.py           # File descriptions for messages; splitting documents into pages/slides/lines for read_file
storage.py         # Per-session file directories (uploads/<session_id>/)
config.py          # Provider configs
system_prompt.txt  # System prompt - after editing, apply it with the "↺ Prompt" button (no restart)
database.py        # SQLite: sessions, messages, saved steps of each turn
Dockerfile.sandbox
static/
  index.html       # HTML structure
  style.css        # All styles
  app.js           # All frontend logic
```

## Requirements

- Python 3.11+
- Docker (for python execution sandbox)
- API keys as needed (see `.env` section above)

---

**Notes:**

- This project has only been tested against the Groq API. Ollama and Gemini configs are present in `config.py` but haven't been verified end-to-end - expect rough edges if you switch providers.
- gpt-oss models on Groq have a native `browser_search` tool that avoids search-related tool-call errors, but it's very token-heavy (a single complex query can burn tens of thousands of tokens - a meaningful chunk of the free tier's daily budget). This app sticks with Tavily's `search_web` instead. Repeated searching turned out to come mostly from results being cut to fit the main model's limit (qwen did it too, not just gpt-oss), so the full results now go to the helper model, which extracts the relevant facts and only that digest reaches the main model. `agent.py` still caps `search_web` at one real call per turn as a safety net.
- Editing a message keeps the session's sandbox and files: the edited message keeps its attachments, and variables from earlier turns stay usable (so can ones from the removed turns).
- Groq's free tier rejects any single request over 8000 tokens, so on `groq` the conversation history is summarized once it passes ~12,000 characters (`context_max_chars` in `config.py`). Attached documents never fill the prompt: only their beginning goes in, later turns just name them, and `read_file` returns at most ~6,000 characters per call.
- Summarising a whole long document is the one file task the free tier handles poorly - it means many `read_file` calls. Questions about specific parts work well (`read_file` with a query).
- Code that runs longer than 30 seconds is interrupted like Ctrl+C in Jupyter: variables and files are kept, and the model is told which line it was stuck on (usually a loop that never ends). Only code stuck inside a library call that can't be interrupted gets the container restarted, which clears that session's variables. Printed output is cut at 5,000 characters per run.
- Rate limits: short per-minute waits (up to 60 s) are waited out with a "Waiting for rate limit" note in chat; the daily token limit ends the turn with an error that says when to retry.
