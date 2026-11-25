"""One place to call the model.

On a Sprite: set LLM_BASE to your OpenRouter connector URL, e.g.
  https://api.sprites.dev/v1/gateway/openrouter/<conn_id>/api/v1
Locally:     set OPENROUTER_API_KEY instead (talks to openrouter.ai directly).
Offline:     LLM_BACKEND=mock
"""
import json
import os
import urllib.error
import urllib.request

LLM_BASE = os.environ.get("LLM_BASE")
API_KEY = os.environ.get("OPENROUTER_API_KEY")
BACKEND = os.environ.get("LLM_BACKEND", "openrouter")
MODEL = os.environ.get("ROAST_MODEL", "anthropic/claude-sonnet-4.5")


def chat(system, user, max_tokens=1200, json_mode=False):
    if BACKEND == "mock":
        return _mock(user, json_mode)
    base = LLM_BASE or "https://openrouter.ai/api/v1"
    headers = {"Content-Type": "application/json"}
    if not LLM_BASE:
        if not API_KEY:
            raise SystemExit("No model configured: set LLM_BASE (Sprite) or OPENROUTER_API_KEY, or LLM_BACKEND=mock")
        headers["Authorization"] = f"Bearer {API_KEY}"
    body = {"model": MODEL, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(f"{base}/chat/completions", json.dumps(body).encode(), headers)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM call failed {e.code}: {e.read().decode(errors='replace')[:300]}") from None


def _mock(user, json_mode):
    if not json_mode:
        return "(mock model reply)"
    try:  # echo back the finding ids so the pipeline can be tested end to end
        ids = json.loads(user.split("FINDINGS_JSON:", 1)[1])
        ids = [f["id"] for f in ids][:6]
    except Exception:
        ids = []
    return json.dumps({
        "opener": "I've seen cleaner code in a ransom note. (mock)",
        "burns": [{"id": i, "burn": f"Mock burn about `{i}`."} for i in ids],
        "closer": "Fix the list below and I'll pretend this never happened. (mock)",
    })
