import base64
from pathlib import Path

MAX_CHARS = 50000

SUPPORTED_EXTENSIONS = {
    "image": ["png", "jpg", "jpeg", "webp", "gif"],
    "pdf":   ["pdf"],
    "notebook": ["ipynb"],
    "excel": ["xlsx", "xls"],
    "csv":   ["csv"],
    "text":  ["md", "txt", "py", "js", "ts", "json", "yaml", "yml", "html", "css"],
}

ALL_SUPPORTED = [ext for exts in SUPPORTED_EXTENSIONS.values() for ext in exts]


def process_file(path: str, filename: str) -> dict:
    ext = filename.split(".")[-1].lower()

    if ext not in ALL_SUPPORTED:
        return {
            "type": "error",
            "content": f"Unspported format '.{ext}'. Supported: {', '.join(ALL_SUPPORTED)}",
            "name": filename
        }

    try:
        if ext in SUPPORTED_EXTENSIONS["image"]:
            return _process_image(path, filename, ext)

        if ext in SUPPORTED_EXTENSIONS["pdf"]:
            return _process_pdf(path, filename)

        if ext in SUPPORTED_EXTENSIONS["notebook"]:
            return _process_notebook(path, filename)

        if ext in SUPPORTED_EXTENSIONS["excel"]:
            result = _process_excel(path, filename)
            result["original_path"] = path
            return result

        if ext in SUPPORTED_EXTENSIONS["csv"]:
            result = _process_csv(path, filename)
            result["original_path"] = path
            return result

        if ext in SUPPORTED_EXTENSIONS["text"]:
            return _process_text(path, filename)

    except Exception as e:
        return {
            "type": "error",
            "content": f"Error reading file: {str(e)}",
            "name": filename
        }


def _process_image(path: str, filename: str, ext: str) -> dict:
    return {"type": "image", "name": filename, "ext": ext, "original_path": path}


def _process_pdf(path: str, filename: str) -> dict:
    try:
        import fitz
    except ImportError:
        return {"type": "error", "content": "pymupdf not installed - use: pip install pymupdf", "name": filename}

    doc = fitz.open(path)
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text()
        if text.strip():
            pages.append(text)

    if not pages:
        return {
            "type": "error",
            "content": "PDF looks like a scan. OCR not implemented.",
            "name": filename
        }

    text = "\n\n".join(pages)
    return {"type": "text", "content": _truncate(text, filename), "name": filename}


def _process_notebook(path: str, filename: str) -> dict:
    try:
        import nbformat
    except ImportError:
        return {"type": "error", "content": "nbformat not installed - use: pip install nbformat", "name": filename}

    nb = nbformat.read(open(path, encoding="utf-8", errors="replace"), as_version=4)
    cells = []

    for cell in nb.cells:
        cells.append(f"[{cell.cell_type}]\n{cell.source}")

        if cell.cell_type == "code" and cell.get("outputs"):
            for out in cell.outputs:
                if "text" in out:
                    cells.append(f"[output]\n{''.join(out.text)}")
                elif "data" in out and "text/plain" in out.data:
                    cells.append(f"[output]\n{out.data['text/plain']}")

    text = "\n\n".join(cells)
    return {"type": "text", "content": _truncate(text, filename), "name": filename}


def _process_excel(path: str, filename: str) -> dict:
    try:
        import pandas as pd
    except ImportError:
        return {"type": "error", "content": "pandas not installed - use: pip install pandas openpyxl", "name": filename}

    xl = pd.ExcelFile(path)
    sheets = []

    for sheet_name in xl.sheet_names:
        df = xl.parse(sheet_name)
        sheets.append(f"### Arkusz: {sheet_name}\n{df.to_markdown(index=False)}")

    text = "\n\n".join(sheets)
    return {"type": "text", "content": _truncate(text, filename), "name": filename}


def _process_csv(path: str, filename: str) -> dict:
    import csv as _csv
    for encoding in ["utf-8", "utf-8-sig", "latin-1", "cp1250"]:
        try:
            content = open(path, encoding=encoding).read()
            try:
                dialect = _csv.Sniffer().sniff(content[:2048], delimiters=",;\t|")
                sep = dialect.delimiter
            except Exception:
                sep = ","
            decimal = "."
            for line in content.splitlines()[1:6]:
                if any("," in p for p in line.split(sep)):
                    decimal = ","
                    break
            hint = f"[CSV: sep='{sep}', decimal='{decimal}']\n"
            return {"type": "text", "content": _truncate(hint + content, filename), "name": filename}
        except UnicodeDecodeError:
            continue
    return {"type": "error", "content": "Could not read the CSV file.", "name": filename}


def _process_text(path: str, filename: str) -> dict:
    text = open(path, encoding="utf-8", errors="replace").read()
    return {"type": "text", "content": _truncate(text, filename), "name": filename}


def _truncate(text: str, filename: str) -> str:
    if len(text) <= MAX_CHARS:
        return text
    return (
        text[:MAX_CHARS]
        + f"\n\n[{filename} – file cut, passed {MAX_CHARS} out of {len(text)}]"
    )