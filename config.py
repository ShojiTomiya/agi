import os

PROVIDER = os.getenv("LLM_PROVIDER", "ollama")

CONFIGS = {
    "groq": {
        "provider": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": os.getenv("GROQ_API_KEY"),
        "model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "vision_model": "meta-llama/llama-4-scout-17b-16e-instruct",
        "max_tokens": 2048,
        "context_max_chars": 60000,
        "summary_recent": 6,
    },
    "ollama": {
        "provider": "ollama",
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "model": "qwen3:8b",
        "vision_model": "qwen2.5vl:7b",
        "max_tokens": 4096,
        "context_max_chars": 60000,
        "summary_recent": 8,
    },
    "gemini": {
        "provider": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key": os.getenv("GEMINI_API_KEY"),
        "model": "gemini-2.0-flash",
        "vision_model": "gemini-2.0-flash",
        "max_tokens": 2048,
        "context_max_chars": 80000,
        "summary_recent": 6,
    },
}

CONFIG = CONFIGS[PROVIDER]
