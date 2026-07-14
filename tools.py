import os
import uuid
import base64
import glob
from openai import OpenAI
from tavily import TavilyClient
from config import CONFIG

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
                "after your training cutoff."
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
                "Execute Python code in a secure sandbox and return the output. "
                "Use for calculations, data analysis, string processing, generating charts, or creating any files. "
                "Input files attached by the user are available in /workspace by their original filenames. "
                "Available libraries: pandas, matplotlib, numpy, scipy, seaborn, openpyxl, pillow, "
                "scikit-learn, requests, reportlab, fpdf2. Do NOT use any other libraries. "
                "For PDFs with Polish/Unicode text ALWAYS register DejaVu font first. "
                "With reportlab: from reportlab.pdfbase import pdfmetrics; from reportlab.pdfbase.ttfonts import TTFont; pdfmetrics.registerFont(TTFont('DejaVu', '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')) then use fontName='DejaVu'. "
                "With fpdf2: pdf=FPDF(); pdf.add_font('DejaVu','','/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'); pdf.set_font('DejaVu',size=12) — never use set_font('helvetica') with non-ASCII text. "
                "Save ALL output files to /workspace/output/. "
                "For charts use plt.show() — captured automatically."
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
            "name": "generate_image",
            "description": (
                "Generate an image from a text description using AI. "
                "Use when the user asks to create, draw, generate, or visualize something as an image. "
                "Write a detailed English prompt for best results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed description of the image to generate"},
                    "width":  {"type": "integer", "description": "Width in pixels (default 1024)"},
                    "height": {"type": "integer", "description": "Height in pixels (default 1024)"},
                    "model":  {"type": "string", "description": "Ignored, kept for compatibility"}
                },
                "required": ["prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_image",
            "description": (
                "Analyze an image file (png, jpg, jpeg, webp, gif) using a vision model. "
                "ONLY call this for actual image files — never for CSV, PDF, Excel, or text files. "
                "Call this when the user asks about an image they have attached. "
                "Ask a specific question to get the most useful response."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Bare filename only, e.g. 'photo.png' — no path or directory prefix (this is unrelated to execute_python's /workspace/ sandbox path)"},
                    "question": {"type": "string", "description": "What to ask about the image"}
                },
                "required": ["filename", "question"]
            }
        }
    }
]



def search_web(query: str, max_results: int = 5) -> str:
    try:
        resp = _tavily.search(query=query, max_results=max_results)
        results = resp.get("results", [])
        if not results:
            return "No results found."
        return "\n\n".join(
            f"[{r['title']}]({r['url']})\n{r['content']}"
            for r in results
        )
    except Exception as e:
        return f"Search failed: {e}"


def execute_python(code: str, context_files: list = None, session_id: str = None) -> dict:
    """Returns {'content': str|list, 'chart_paths': [...], 'file_paths': [...]}"""
    import kernel

    if not session_id:
        return {'content': 'Error: no session_id for kernel', 'chart_paths': [], 'file_paths': []}

    result = kernel.execute(session_id, code, context_files)

    stdout = (result.get('stdout') or '').strip()
    error = result.get('error')
    images = result.get('images', [])
    output_files = result.get('output_files', [])

    chart_paths = []
    file_paths = []
    content_parts = []

    text_out = stdout
    if error:
        text_out = (text_out + '\nError:\n' + error).strip()
    if text_out:
        content_parts.append({'type': 'text', 'text': text_out})

    for img in images:
        local_path = f"uploads/{uuid.uuid4()}_{img['name']}"
        with open(local_path, 'wb') as f:
            f.write(base64.b64decode(img['data']))
        chart_paths.append(local_path)
        content_parts.append({'type': 'text', 'text': f"[generated image: '{img['name']}' — call analyze_image('{img['name']}', 'describe this chart') to inspect it]"})

    for of in output_files:
        local_path = f"uploads/{uuid.uuid4()}_{of['name']}"
        with open(local_path, 'wb') as f:
            f.write(base64.b64decode(of['data']))
        ext = of.get('ext', '')
        if ext in ('png', 'jpg', 'jpeg'):
            chart_paths.append(local_path)
            content_parts.append({'type': 'text', 'text': f"[generated image: '{of['name']}' — call analyze_image('{of['name']}', 'describe this chart') to inspect it]"})
        else:
            file_paths.append({'path': local_path, 'name': of['name']})
            content_parts.append({'type': 'text', 'text': f"[file generated: '{of['name']}' — available for download]"})

    content = '\n'.join(p['text'] for p in content_parts).strip() or '(no output)'
    return {'content': content, 'chart_paths': chart_paths, 'file_paths': file_paths}


def generate_image(prompt: str, width: int = 1024, height: int = 1024, model: str = "flux") -> dict:
    import requests as req
    hf_key = os.getenv("HF_API_KEY")
    if not hf_key:
        return {"content": "HF_API_KEY not set in .env", "chart_paths": [], "file_paths": []}
    url = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-dev"
    try:
        resp = req.post(
            url,
            headers={"Authorization": f"Bearer {hf_key}"},
            json={"inputs": prompt, "parameters": {"width": width, "height": height, "num_inference_steps": 28}},
            timeout=120,
        )
        resp.raise_for_status()
    except Exception as e:
        return {"content": f"Image generation failed: {e}", "chart_paths": [], "file_paths": []}
    path = f"uploads/{uuid.uuid4()}_generated.png"
    with open(path, "wb") as f:
        f.write(resp.content)
    return {"content": f"Generated image: {prompt}", "chart_paths": [path], "file_paths": []}


def analyze_image(filename: str, question: str, context_files: list = None) -> str:
    filename = os.path.basename(filename)
    image_path = None

    for att in (context_files or []):
        if att.get("name") == filename and att.get("type") == "image":
            p = att.get("original_path")
            if p and os.path.exists(p):
                image_path = p
                break

    if not image_path:
        matches = glob.glob(f"uploads/*_{filename}")
        if matches:
            image_path = sorted(matches)[-1]

    if not image_path or not os.path.exists(image_path):
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext not in ("png", "jpg", "jpeg", "webp", "gif"):
            return f"'{filename}' is not an image file. analyze_image only works with png/jpg/jpeg/webp/gif. Use execute_python to work with this file."
        return f"Image '{filename}' not found."

    ext = image_path.rsplit(".", 1)[-1].lower()
    media_type = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
    b64 = base64.b64encode(open(image_path, "rb").read()).decode()

    try:
        resp = _client.chat.completions.create(
            model=CONFIG["vision_model"],
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                    {"type": "text", "text": question},
                ]
            }],
            max_tokens=1024,
        )
        return resp.choices[0].message.content
    except Exception as e:
        return f"Vision analysis failed: {e}"


def execute_tool(name: str, args: dict, attachments: list = None, session_id: str = None) -> dict | str:
    if name == "search_web":
        return search_web(args.get("query", ""))
    if name == "generate_image":
        return generate_image(args.get("prompt", ""), args.get("width", 1024), args.get("height", 1024), args.get("model", "flux"))
    if name == "execute_python":
        return execute_python(args.get("code", ""), context_files=attachments, session_id=session_id)
    if name == "analyze_image":
        return analyze_image(args.get("filename", ""), args.get("question", "Describe this image."), context_files=attachments)
    return f"Unknown tool: {name}"
