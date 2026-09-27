"""The ONLY module that talks to an LLM provider (Gemini). Everything else calls these
functions, so switching provider means rewriting this one file.

Rules for callers:
  - the LLM never makes a decision the engine can make; it reads text or phrases answers;
  - every answer is validated by the caller (schema here, content checks there);
  - answers are cached by input hash, so a re-run costs nothing and gives the same result.

The key comes from GEMINI_API_KEY in .env (git-ignored). Without a key, `available()` is
False and callers skip LLM steps instead of failing.
"""
import hashlib
import json
import os
import time
from typing import TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel

from recommender.config import ENV_FILE, LLM_CACHE, LLM_MODEL_EXTRACT

T = TypeVar("T", bound=BaseModel)
RETRY_CODES = {429, 500, 503}      # rate limit / transient server errors
_client = None


class LLMUnavailable(RuntimeError):
    """No API key configured."""


def client():
    global _client
    if _client is None:
        load_dotenv(ENV_FILE)
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise LLMUnavailable(f"GEMINI_API_KEY is not set (add it to {ENV_FILE.name})")
        from google import genai
        _client = genai.Client(api_key=key)
    return _client


def available() -> bool:
    try:
        client()
        return True
    except LLMUnavailable:
        return False


def _cache() -> dict:
    return json.loads(LLM_CACHE.read_text(encoding="utf-8")) if LLM_CACHE.exists() else {}


def _cache_put(key: str, value: dict) -> None:
    cache = _cache()
    cache[key] = value
    LLM_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False), encoding="utf-8")


def extract(prompt: str, schema: type[T], *, system: str | None = None, pdf: bytes | None = None,
            model: str = LLM_MODEL_EXTRACT, retries: int = 4) -> T:
    """Structured extraction: the model must answer with JSON matching `schema`.

    `pdf` sends a document instead of text (used for scanned handouts). Temperature 0 and the
    cache keep results stable across runs.
    """
    from google.genai import errors, types

    key = hashlib.sha256("\x1f".join([model, schema.__name__, system or "", prompt]).encode()
                         + (pdf or b"")).hexdigest()
    cached = _cache().get(key)
    if cached is not None:
        return schema.model_validate(cached)

    contents = [types.Part.from_bytes(data=pdf, mime_type="application/pdf"), prompt] if pdf else prompt
    config = types.GenerateContentConfig(system_instruction=system, temperature=0,
                                         response_mime_type="application/json", response_schema=schema)
    for attempt in range(retries):
        try:
            resp = client().models.generate_content(model=model, contents=contents, config=config)
            result = resp.parsed if isinstance(resp.parsed, schema) else schema.model_validate_json(resp.text)
            _cache_put(key, result.model_dump())
            return result
        except errors.APIError as e:
            if e.code in RETRY_CODES and attempt < retries - 1:
                time.sleep(5 * 2 ** attempt)       # 5, 10, 20 s
                continue
            raise


class ChatSession:
    """A multi-turn conversation where the model may call Python functions (tools).

    The SDK's automatic function calling runs the tools and feeds results back until the model
    writes a text answer. Transient errors (rate limit / overload) are retried with backoff; if the
    model stays overloaded, the conversation moves to the next model in LLM_AGENT_FALLBACKS,
    keeping its history.
    """

    def __init__(self, system: str, tools: list, model: str | None = None, max_tool_calls: int = 12):
        from google.genai import types
        from recommender.config import LLM_AGENT_FALLBACKS, LLM_MODEL_AGENT
        self._config = types.GenerateContentConfig(
            system_instruction=system, tools=tools, temperature=0.2,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=max_tool_calls))
        self.models = [model or LLM_MODEL_AGENT, *LLM_AGENT_FALLBACKS]
        self.model = self.models[0]
        self._chat = client().chats.create(model=self.model, config=self._config)

    def send(self, message: str, retries: int = 2) -> str:
        from google.genai import errors
        for model in self.models[self.models.index(self.model):]:
            if model != self.model:            # switch model, keep the conversation so far
                self._chat = client().chats.create(model=model, config=self._config,
                                                   history=self._chat.get_history())
                self.model = model
            for attempt in range(retries):
                try:
                    return self._chat.send_message(message).text or ""
                except errors.APIError as e:
                    if e.code not in RETRY_CODES:
                        raise
                    if attempt < retries - 1:
                        time.sleep(3 * 2 ** attempt)
        raise RuntimeError("All configured Gemini models are unavailable right now; please try again shortly.")
