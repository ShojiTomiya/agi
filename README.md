# Local AI chat

A self-hosted AI chat app with an agentic loop, streaming responses and python execution sandbox. Built with FastAPI, SQLite, and vanilla JS.

## Features

- **Multi-provider support** - Groq, Ollama (local), Gemini
- **Streaming responses** - tokens rendered in real time with Markdown and LaTeX (KaTeX)
- **Python sandbox** - Docker kernel per session; variables survive between messages, matplotlib charts render inline
- **Web search** - via Tavily API
- **Image generation** - FLUX.1-dev via HuggingFace free tier
- **Image analysis** - vision model for uploaded images and generated charts
- **File attachments** - PDF, Excel, CSV, Jupyter notebooks, images
- **Loop inspector**- sidebar showing each LLM call, tool call, and result
- **Session management** - history, rename, delete, edit messages

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

# Image generation (https://huggingface.co/settings/tokens)
HF_API_KEY=hf_your_key_here
```

For **Ollama**, no API key is needed - just make sure Ollama is running locally (`ollama serve`).

### 3. Build the sandbox image

The Python execution sandbox runs in Docker. Build the image once:

```bash
docker build -f Dockerfile.sandbox -t agi-sandbox:latest .
```

The image includes: pandas, matplotlib, numpy, scipy, seaborn, scikit-learn, reportlab, fpdf2, openpyxl, Pillow, and DejaVu fonts (for Unicode/Polish text in PDFs).

### 4. Run

```bash
uvicorn main:app --reload
```

Open [http://localhost:8000](http://localhost:8000).

## Provider configuration

Switch providers by setting `LLM_PROVIDER` in `.env`. Models and parameters are configured in `config.py`.

| Provider | Models | Notes |
|----------|--------|-------|
| `groq` | llama-4-scout, etc. | Fast, free tier available |
| `ollama` | qwen3:8b, qwen2.5vl:7b | Fully local, no API key |
| `gemini` | gemini-2.0-flash | Free tier via AI Studio |

## Python sandbox

Each chat session gets a dedicated Docker container with a python namespace - variables defined in one message are available in the next, like a Jupyter notebook. The model can:

- Run calculations and data analysis
- Generate charts (`plt.show()` captures them automatically)
- Create files (Excel, PDF, CSV) - available for download in chat
- Read files attached by the user (`/workspace/<filename>`)

Output files are returned as download buttons in the assistant's response.

## Project structure

```
main.py            # FastAPI app, routes
agent.py           # Agentic loop with streaming
tools.py           # Tool definitions and implementations
kernel.py          # Docker sandbox lifecycle management
repl_server.py     # HTTP REPL server running inside the container
context.py         # Message history, system prompt, token management
files.py           # File reading (PDF, Excel, CSV, images, notebooks)
config.py          # Provider configs
database.py        # SQLite session and message storage
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
