"""Thin Groq client (OpenAI-compatible chat completions) with retries and robust JSON parsing."""
import json
import re
import time
from typing import Optional

import requests

from .config import Config

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class LLMError(RuntimeError):
    pass


class GroqLLM:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _post(self, model: str, messages: list, temperature: float, json_mode: bool, max_tokens: int):
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_completion_tokens": max_tokens,
        }
        if model.startswith("openai/gpt-oss-"):
            payload["reasoning_effort"] = "low"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        return requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {self.cfg.groq_api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=35,
        )

    def complete(self, system: str, user: str, *, history: Optional[list] = None, temperature: float = 0.4,
                 json_mode: bool = False, max_tokens: int = 1800) -> str:
        messages = [{"role": "system", "content": system}]
        messages += history or []
        messages.append({"role": "user", "content": user})

        models = [self.cfg.llm_model]
        if self.cfg.llm_fallback_model and self.cfg.llm_fallback_model != self.cfg.llm_model:
            models.append(self.cfg.llm_fallback_model)

        last_err = ""
        for model in models:
            for attempt in range(3):
                try:
                    r = self._post(model, messages, temperature, json_mode, max_tokens)
                except requests.RequestException:
                    last_err = "network error or timeout"
                    time.sleep(2 * (attempt + 1))
                    continue
                if r.status_code == 200:
                    try:
                        content = r.json()["choices"][0]["message"]["content"]
                        if isinstance(content, str) and content.strip():
                            if json_mode:
                                parse_json(content)
                            return content
                    except (ValueError, KeyError, IndexError, TypeError, LLMError):
                        pass
                    last_err = "empty or malformed response"
                    continue
                last_err = f"HTTP {r.status_code}"
                if r.status_code == 429:  # rate limited -> wait, then retry / fall back
                    time.sleep(min(20, 5 * (attempt + 1)))
                    continue
                if r.status_code >= 500:
                    time.sleep(2 * (attempt + 1))
                    continue
                break  # 4xx other than 429: do not retry this model
        raise LLMError(f"Groq call failed: {last_err}")

    def complete_json(self, system: str, user: str, **kw) -> dict:
        raw = self.complete(system, user, json_mode=True, **kw)
        return parse_json(raw)


def parse_json(raw: str) -> dict:
    """Parse model output as JSON, tolerating code fences and stray text around the object."""
    raw = raw.strip()
    fenced = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(fenced)
    except json.JSONDecodeError:
        pass
    else:
        if isinstance(parsed, dict):
            return parsed
        raise LLMError("Model JSON must contain an object")
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            pass
    raise LLMError("Model did not return valid JSON")
