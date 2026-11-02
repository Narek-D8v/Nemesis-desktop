"""Список моделей Gemini, поддерживающих bidiGenerateContent (Live API)."""

from __future__ import annotations

import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    from nemus.config import load_config

    key = load_config().llm.providers.get("gemini").api_key
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}&pageSize=200"
    with urllib.request.urlopen(url, timeout=30) as response:
        data = json.load(response)
    for model in data.get("models", []):
        methods = model.get("supportedGenerationMethods") or []
        if "bidiGenerateContent" in methods:
            print(model.get("name"), "|", model.get("displayName"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
