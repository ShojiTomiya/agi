import os

PROVIDER = os.getenv("LLM_PROVIDER", "ollama")

CONFIGS = {
    "groq": {
        "provider": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": os.getenv("GROQ_API_KEY"),
        "model": "openai/gpt-oss-120b",
        # on this app's tasks "low" was as accurate as the default "medium", and faster
        "model_extra_body": {"reasoning_effort": "low"},
        "vision_model": "qwen/qwen3.8-27b",
        # thinking mode made image descriptions ~15x slower without making them better
        "vision_extra_body": {"reasoning_effort": "none"},
        # Groq rate-limits each model separately, so cheap work (condensing search
        # results, summarizing old history) runs here, off the main model's budget
        "helper_model": "openai/gpt-oss-20b",
        "helper_extra_body": {"reasoning_effort": "low"},
        "max_tokens": 4096,
        # the free tier rejects any single request over 8000 tokens, so history is
        # summarized well below that
        "context_max_chars": 12000,
        "summary_recent": 6,
    },
    "ollama": {
        "provider": "ollama",
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "model": "qwen3:8b",
        "vision_model": "qwen2.5vl:7b",
        "helper_model": "qwen3:8b",
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
        "helper_model": "gemini-2.0-flash",
        "max_tokens": 2048,
        "context_max_chars": 80000,
        "summary_recent": 6,
    },
}

CONFIG = CONFIGS[PROVIDER]
