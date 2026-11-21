"""Talking to the AI service. You bring your own key, and choose the service and model in the dashboard
(Settings, AI) or in .env. What is saved from the dashboard is kept in ai.json in the data folder and wins.

    PEAKCUT_AI_PROVIDER   gemini | openai | xai | groq | openrouter | anthropic | ollama | custom
    PEAKCUT_AI_MODELS     one or more model ids, comma separated, tried in this order
    PEAKCUT_AI_KEY        your key (or PEAKCUT_AI_KEY_FILE, a file holding it)
    PEAKCUT_AI_BASE_URL   only for "custom" (any service that speaks the OpenAI chat format), or to override
    PEAKCUT_AI_EFFORT     how hard Claude or OpenAI thinks first: low, medium, high, xhigh, max (empty: the model's own)
    PEAKCUT_AI_BACKUP_*   the same settings for a second service, used when the first fails or has no key

Every service is asked for the same thing: JSON that follows a small schema. Gemini takes the schema directly.
The others are told the schema in the prompt and asked for a JSON object, which works with any chat model."""
import json
import os
import re
import threading

import httpx

from . import config

# name -> (label shown on the pages, address, how it is spoken to, where to get a key, an example model id)
PROVIDERS = {
    "gemini": ("Gemini", "https://generativelanguage.googleapis.com/v1beta/models", "gemini", "https://aistudio.google.com/apikey", "gemini-2.5-flash"),
    "openai": ("OpenAI", "https://api.openai.com/v1", "openai", "https://platform.openai.com/api-keys", "gpt-4o-mini"),
    "xai": ("Grok", "https://api.x.ai/v1", "openai", "https://console.x.ai", "grok-4"),
    "groq": ("Groq", "https://api.groq.com/openai/v1", "openai", "https://console.groq.com/keys", "llama-3.3-70b-versatile"),
    "openrouter": ("OpenRouter", "https://openrouter.ai/api/v1", "openai", "https://openrouter.ai/keys", "google/gemini-2.5-flash"),
    "anthropic": ("Claude", "https://api.anthropic.com/v1", "anthropic", "https://console.anthropic.com", ""),
    "ollama": ("Ollama (on this machine)", "http://127.0.0.1:11434/v1", "openai", "https://ollama.com", "llama3.1"),
    "custom": ("Custom (OpenAI-compatible)", "", "openai", "", ""),
}
ALIASES = {"google": "gemini", "grok": "xai", "claude": "anthropic", "local": "ollama"}
NO_KEY = {"ollama", "custom"}       # a model running on your own machine needs no key

# ---------------------------------------------------------------- the settings in use
_lock = threading.Lock()
_saved = {"at": None, "data": {}}


def _file():
    return config.DATA_DIR / "ai.json"


def _from_dashboard() -> dict:
    """What was saved from the dashboard, read again only when the file changes."""
    f = _file()
    try:
        at = f.stat().st_mtime_ns
    except OSError:
        at = 0
    if _saved["at"] != at:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if at else {}
        except (OSError, ValueError):
            data = {}
        _saved.update(at=at, data=data if isinstance(data, dict) else {})
    return _saved["data"]


SLOTS = ("main", "backup")          # the AI tried first, and the one used when it fails or has no key
EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _env(slot):
    if slot == "backup":
        return config.AI_BACKUP_PROVIDER, config.AI_BACKUP_MODELS, config.AI_BACKUP_BASE_URL, config.ai_backup_key(), config.AI_BACKUP_EFFORT
    return config.AI_PROVIDER, config.AI_MODELS, config.AI_BASE_URL, config.ai_key(), config.AI_EFFORT


def current(slot="main") -> dict:
    """Service, models, address, key and thinking effort for one slot: what the dashboard saved, or else .env."""
    d = _from_dashboard()
    if slot == "backup":
        d = d.get("backup") if isinstance(d.get("backup"), dict) else {}
    ep, em, eb, ek, ee = _env(slot)
    if d.get("off"):
        ep, em, eb, ek, ee = "", [], "", "", ""
    saved = bool(d.get("provider"))
    p = str(d.get("provider") or ep or "").strip().lower()
    effort = str(d["effort"] if saved and "effort" in d else ee or "").strip().lower()
    return {"slot": slot, "provider": ALIASES.get(p, p),
            "models": list(d["models"]) if saved else list(em),
            "base_url": (str(d.get("base_url") or "") if saved else eb).rstrip("/"),
            "key": str(d["key"]) if saved and "key" in d else ek,
            "effort": effort if effort in EFFORTS else ""}


def _base(c) -> str:
    return c["base_url"] or PROVIDERS.get(c["provider"], ("", ""))[1]


def _ready(c) -> bool:
    return bool(c["provider"] in PROVIDERS and c["models"] and _base(c) and (c["key"] or c["provider"] in NO_KEY))


def services() -> list:
    """The AI services that can be used, in the order they are tried: the main one, then the backup."""
    return [c for c in (current("main"), current("backup")) if _ready(c)]


def _first() -> dict:
    return (services() or [current("main")])[0]


def provider() -> str:
    return _first()["provider"]


def models() -> list:
    return _first()["models"]


def key() -> str:
    return _first()["key"]


def label(c=None) -> str:
    return PROVIDERS.get((c or _first())["provider"], ("",))[0]


def base_url() -> str:
    return _base(_first())


def off() -> bool:
    """True when the owner switched the AI off in Settings: no AI calls at all until it is switched on again."""
    return bool(_from_dashboard().get("off"))


def set_off(value: bool) -> dict:
    with _lock:
        data = dict(_from_dashboard())
    data["off"] = bool(value)
    _write(data)
    return public()


def ready() -> bool:
    """True when at least one AI service has what it needs, and the AI is switched on."""
    return bool(services()) and not off()


def hide_keys(text: str) -> str:
    """Text with every key in use blanked out, safe to show or log."""
    for slot in SLOTS:
        k = current(slot)["key"]
        if k:
            text = text.replace(k, "<key>")
    return text


def _public(c) -> dict:
    return {"provider": c["provider"], "models": c["models"], "base_url": c["base_url"], "has_key": bool(c["key"]),
            "key_hint": c["key"][-4:] if len(c["key"]) >= 12 else "", "ready": _ready(c), "label": label(c) if c["provider"] else "",
            "effort": c["effort"]}


def public() -> dict:
    """What the settings page may see. The keys themselves never leave the server: only whether there is one, and
    its last four characters so the owner can tell which key it is."""
    main, back = current("main"), current("backup")
    first = _first()
    return {**_public(main), "ready": ready(), "off": off(), "in_use": {"label": label(first), "model": (first["models"] or [""])[0]} if services() else None,
            "saved_here": bool(_from_dashboard().get("provider")), "backup": _public(back), "efforts": list(EFFORTS),
            "providers": [{"id": k, "label": v[0], "address": v[1], "needs_key": k not in NO_KEY, "key_url": v[3], "example": v[4]}
                          for k, v in PROVIDERS.items()]}


def save(provider_id: str, model_ids, address: str = "", new_key=None, slot="main", effort=None) -> dict:
    """Save the choice made in the dashboard for one slot. `new_key` None keeps the key that is already there (the
    page never has it to send back); an empty string removes it. The backup slot can be switched off with an empty
    service."""
    slot = slot if slot in SLOTS else "main"
    raw = str(provider_id or "").strip().lower()
    with _lock:
        data = dict(_from_dashboard())
    if slot == "backup" and raw in ("", "none", "off"):
        data["backup"] = {"off": True}
        _write(data)
        return public()
    p = ALIASES.get(raw, raw)
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    if isinstance(model_ids, str):
        model_ids = model_ids.split(",")
    ms = [str(m).strip()[:120] for m in (model_ids or []) if str(m).strip()][:5]
    if not ms:
        raise ValueError("Enter the model to use.")
    if any(not re.fullmatch(r"[\w.\-:/@]+", m) for m in ms):
        raise ValueError("A model id has letters, digits and . - : / only.")
    address = str(address or "").strip().rstrip("/")[:300]
    if address and not re.fullmatch(r"https?://[^\s]+", address):
        raise ValueError("The address has to start with http:// or https://")
    if p == "custom" and not address:
        raise ValueError("A custom service needs its address, for example http://127.0.0.1:1234/v1")
    old = current(slot)
    k = old["key"] if new_key is None and old["provider"] == p else str(new_key or "").strip()[:400]
    if not k and p not in NO_KEY:
        raise ValueError("Paste the key for this service.")
    e = old["effort"] if effort is None else str(effort or "").strip().lower()
    if e and e not in EFFORTS:
        raise ValueError("Thinking is one of: " + ", ".join(EFFORTS) + ".")
    entry = {"provider": p, "models": ms, "base_url": address, "key": k, "effort": e}
    if slot == "backup":
        data["backup"] = entry
    else:
        data.update(entry)
    _write(data)
    return public()


def _write(data):
    with _lock:
        f = _file()
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)        # the keys are only for whoever runs the server
        except OSError:
            pass
        tmp.replace(f)
        _saved["at"] = None


def list_models(provider_id=None, address=None, with_key=None, slot="main") -> list:
    """The model ids a service offers, so the settings page can show a list to pick from."""
    c = current(slot if slot in SLOTS else "main")
    p = ALIASES.get(str(provider_id or c["provider"]).lower(), str(provider_id or c["provider"]).lower())
    if p not in PROVIDERS:
        raise ValueError("Choose one of the AI services in the list.")
    k = str(with_key or "").strip() or (c["key"] if c["provider"] == p else "")
    base = str(address or "").strip().rstrip("/") or (c["base_url"] if c["provider"] == p else "") or PROVIDERS[p][1]
    if not base:
        raise ValueError("Enter the service's address first.")
    how = PROVIDERS[p][2]
    if how == "gemini":
        r = httpx.get(base, params={"pageSize": 200}, headers={"x-goog-api-key": k}, timeout=20)
    elif how == "anthropic":
        r = httpx.get(f"{base}/models", params={"limit": 200}, headers={"x-api-key": k, "anthropic-version": "2023-06-01"}, timeout=20)
    else:
        r = httpx.get(f"{base}/models", headers={"Authorization": f"Bearer {k}"} if k else {}, timeout=20)
    if r.status_code != 200:
        raise AIError(f"HTTP {r.status_code} {r.text[:200].replace(k or chr(0), '<key>')}")
    data = r.json()
    if how == "gemini":
        found = [m.get("name", "").split("/")[-1] for m in data.get("models", [])
                 if "generateContent" in (m.get("supportedGenerationMethods") or [])]
    else:
        found = [m.get("id", "") for m in data.get("data", []) if isinstance(m, dict)]
    return sorted({m for m in found if m})[:400]


def test(slot=None) -> dict:
    """One tiny request with one slot's settings (the first usable one if none is named), to prove the key, the
    address and the model all work."""
    c = current(slot) if slot in SLOTS else _first()
    if not _ready(c):
        raise ValueError("Save a service, a model and a key first.")
    m = c["models"][0]
    out, sent, wrote = call(m, 'Answer with a JSON array that holds the single string "ok".', {"type": "ARRAY", "items": {"type": "STRING"}}, c)
    return {"ok": True, "model": m, "label": label(c), "tokens": sent + wrote, "answer": str(out)[:80]}


class AIError(RuntimeError):
    pass


problem = {"text": "", "at": 0.0}       # why the AI last failed, for the dashboard; cleared when it answers again


def explain(e, c=None) -> str:
    """What went wrong with an AI call, in words the owner can act on."""
    c = c or _first()
    text, low = str(e), str(e).lower()
    name = (c["models"] or ["?"])[0]
    if "404" in text or "not found" in low or "does not exist" in low or "unknown model" in low or "not_found" in low:
        return f"{label(c)} doesn't know the model \"{name}\". Open Settings, AI, press Find models and pick one from the list."
    if "401" in text or "403" in text or "api key" in low or "api_key" in low or "unauthorized" in low or "permission" in low:
        return f"{label(c)} refused the key. Paste a working key under Settings, AI."
    if "429" in text or "quota" in low or "rate limit" in low or "exhausted" in low:
        return f"{label(c)} says the key has hit its limit or has no credit left. Check the account with {label(c)}, or try again later."
    if "connect" in low or "timed out" in low or "timeout" in low or "getaddrinfo" in low or "refused" in low:
        return f"PeakCut couldn't reach {label(c)}. Check the internet connection" + (" and that Ollama is running." if c["provider"] == "ollama" else ".")
    return ""


def note_problem(e, c=None):
    import time
    why = explain(e, c)
    if why:
        problem.update(text=why, at=time.time())


def clear_problem():
    problem.update(text="", at=0.0)


def _fail(r, model):
    text = hide_keys(r.text[:300])
    raise AIError(f"HTTP {r.status_code} {text}")


# ---------------------------------------------------------------- the schema, said in words for chat models
def _json_schema(s):
    """Gemini's schema spelling (ARRAY, OBJECT...) as ordinary JSON Schema."""
    if isinstance(s, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _json_schema(v)) for k, v in s.items()}
    if isinstance(s, list):
        return [_json_schema(x) for x in s]
    return s


def _wrapped(prompt, schema):
    return (prompt + "\n\nAnswer with JSON only, no other text and no code fences: one object with a single key "
            "\"items\", whose value follows this JSON Schema:\n" + json.dumps(_json_schema(schema)))


def _parse(text):
    """The JSON in an answer, even if the model wrapped it in a code fence or a sentence."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        out = json.loads(text)
    except ValueError:
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        if not m:
            raise AIError("The AI did not answer with JSON.")
        out = json.loads(m.group(0))
    if isinstance(out, dict) and "items" in out:
        out = out["items"]
    return out


# ---------------------------------------------------------------- one call
def call(model: str, prompt: str, schema: dict, c=None):
    """Ask one model of one service (the first usable one if none is given).
    Returns (the parsed JSON answer, tokens sent, tokens written back)."""
    c = c or _first()
    how = PROVIDERS[c["provider"]][2]
    return {"gemini": _gemini, "openai": _openai, "anthropic": _anthropic}[how](c, model, prompt, schema)


def _gemini(c, model, prompt, schema):
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4, "responseMimeType": "application/json", "responseSchema": schema}}
    r = httpx.post(f"{_base(c)}/{model}:generateContent", json=body, headers={"x-goog-api-key": c["key"]}, timeout=120)
    if r.status_code != 200:
        _fail(r, model)
    data = r.json()
    text = "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])
    u = data.get("usageMetadata", {})
    sent = int(u.get("promptTokenCount") or 0)
    return json.loads(text), sent, max(0, int(u.get("totalTokenCount") or 0) - sent)   # the answer, and any thinking billed with it


def _openai(c, model, prompt, schema):
    headers = {"Authorization": f"Bearer {c['key']}"} if c["key"] else {}
    body = {"model": model, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    effort = c["effort"] if c["provider"] == "openai" else ""
    if effort:
        body["reasoning_effort"] = effort         # thinking first, like Claude's effort

    def send():
        # Most services accept these two extras; a few models refuse one of them, so they are dropped on a refusal.
        r = None
        for extra in ({"temperature": 0.4, "response_format": {"type": "json_object"}}, {"response_format": {"type": "json_object"}}, {}):
            r = httpx.post(f"{_base(c)}/chat/completions", json={**body, **extra}, headers=headers, timeout=600 if effort else 180)
            if r.status_code not in (400, 422):
                break
        return r
    r = send()
    if r.status_code in (400, 422) and effort in ("xhigh", "max") and "reasoning" in r.text.lower():
        body["reasoning_effort"] = "high"         # this model does not offer that much thinking: the most it does
        r = send()
    if r.status_code != 200:
        _fail(r, model)
    data = r.json()
    u = data.get("usage") or {}
    return _parse(data["choices"][0]["message"].get("content")), int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


def _anthropic(c, model, prompt, schema):
    body = {"model": model, "max_tokens": 8192, "messages": [{"role": "user", "content": _wrapped(prompt, schema)}]}
    effort = c["effort"]
    if effort:
        # Thinking first: the model works out its answer, then writes it. Thinking is paid like any written text,
        # so it gets more room to write.
        body.update(thinking={"type": "adaptive"}, output_config={"effort": effort}, max_tokens=32000)
    headers = {"x-api-key": c["key"], "anthropic-version": "2023-06-01"}
    r = httpx.post(f"{_base(c)}/messages", json=body, timeout=600 if effort else 180, headers=headers)
    if r.status_code == 400 and effort in ("xhigh", "max") and "effort" in r.text.lower():
        # this model does not offer that much thinking: use the most it does offer rather than failing
        body["output_config"] = {"effort": "high"}
        r = httpx.post(f"{_base(c)}/messages", json=body, timeout=600, headers=headers)
    if r.status_code != 200:
        _fail(r, model)
    data = r.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage") or {}
    return _parse(text), int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)
