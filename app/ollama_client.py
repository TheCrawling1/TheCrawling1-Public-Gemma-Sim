"""Ollama HTTP client.

Streaming uses the /api/chat NDJSON endpoint. The client surfaces real
server errors instead of letting bare HTTPError propagate so the UI can
show what went wrong, and logs Ollama's timing breakdown (prompt eval,
generation, tokens/sec) on every completed request.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterator

import requests
from flask import current_app


log = logging.getLogger("gemmasim.ollama")


def _cfg() -> dict[str, Any]:
    return current_app.config.get("ollama", {}) or {}


def _host(host: str | None = None) -> str:
    h = host or _cfg().get("host") or "http://127.0.0.1:11434"
    return h.rstrip("/")


# ---------------------------------------------------------------------------
# Discovery / health
# ---------------------------------------------------------------------------


def list_models(host: str | None = None) -> list[dict[str, Any]]:
    try:
        r = requests.get(f"{_host(host)}/api/tags", timeout=5)
        r.raise_for_status()
        data = r.json()
        return [m for m in data.get("models", []) if isinstance(m, dict)]
    except Exception:
        return []


def list_loaded(host: str | None = None) -> list[dict[str, Any]]:
    """Models currently loaded in memory (Ollama /api/ps)."""
    try:
        r = requests.get(f"{_host(host)}/api/ps", timeout=5)
        r.raise_for_status()
        data = r.json()
        return [m for m in data.get("models", []) if isinstance(m, dict)]
    except Exception:
        return []


def warmup(model: str | None = None, host: str | None = None) -> dict[str, Any]:
    """Load a model into memory by sending a tiny generate request.

    Returns {ok, elapsed_seconds, error?}.
    """
    import time
    cfg = _cfg()
    target_model = model or cfg.get("model")
    if not target_model:
        return {"ok": False, "error": "No model configured."}
    target_host = _host(host)

    # Reuse the global default options (esp. num_ctx) so the loaded instance
    # matches what subsequent /api/chat calls will use; otherwise Ollama
    # would reload on the first real message.
    options = dict(cfg.get("default_options") or {})
    payload = {
        "model": target_model,
        "prompt": "",
        "stream": False,
        "keep_alive": cfg.get("keep_alive", -1),
        "options": options,
    }
    started = time.monotonic()
    log.info("warmup model=%s num_ctx=%s", target_model, options.get("num_ctx"))
    try:
        r = requests.post(f"{target_host}/api/generate", json=payload, timeout=600)
    except Exception as e:
        return {"ok": False, "error": str(e), "elapsed_seconds": time.monotonic() - started}
    elapsed = time.monotonic() - started
    if not r.ok:
        return {"ok": False, "error": f"HTTP {r.status_code} — {r.text[:200]}", "elapsed_seconds": elapsed}
    log.info("warmup done in %.2fs", elapsed)
    return {"ok": True, "elapsed_seconds": elapsed, "model": target_model}


def model_names(host: str | None = None) -> list[str]:
    return [m["name"] for m in list_models(host) if m.get("name")]


def test_connection(host: str | None = None, model: str | None = None) -> dict[str, Any]:
    target = _host(host)
    out: dict[str, Any] = {
        "ok": False,
        "host": target,
        "models": [],
        "model": model,
        "model_present": None,
        "error": None,
    }
    try:
        r = requests.get(f"{target}/api/tags", timeout=5)
    except requests.exceptions.ConnectionError:
        out["error"] = f"Cannot reach {target}. Is Ollama running?"
        return out
    except requests.exceptions.Timeout:
        out["error"] = f"Timed out reaching {target}."
        return out
    except Exception as e:
        out["error"] = f"Request failed: {e}"
        return out

    if not r.ok:
        out["error"] = f"HTTP {r.status_code} from {target}/api/tags"
        return out

    try:
        data = r.json()
    except ValueError:
        out["error"] = "Server response was not JSON."
        return out

    models = [m for m in data.get("models", []) if isinstance(m, dict)]
    out["models"] = [m["name"] for m in models if m.get("name")]
    out["ok"] = True
    if model:
        out["model_present"] = model in out["models"]
    return out


# ---------------------------------------------------------------------------
# Streaming chat
# ---------------------------------------------------------------------------


def _loop_period(tail: str, *, min_period: int = 20, reps: int = 4) -> int | None:
    """Detect a runaway exact-repetition loop at the END of `tail`.

    Returns the repeating period length if the last `reps` segments of
    some period p (min_period ≤ p ≤ len(tail)//reps) are byte-identical
    — i.e. the model has locked into `XXXX`. A 4× repeat of a 20+ char
    chunk is never legitimate prose (the multi-response presence spiral
    `…wait, the prompt had Risa… ×30` is the motivating case); short
    intentional repetition ("ha ha ha", "no no no") stays well under
    the 20-char floor. Returns None when no loop is present.
    """
    n = len(tail)
    max_period = n // reps
    for p in range(min_period, max_period + 1):
        seg = tail[n - p:]
        if all(tail[n - (k + 1) * p: n - k * p] == seg for k in range(1, reps)):
            return p
    return None


def chat_stream(
    *,
    system: str,
    messages: list[dict[str, str]],
    model: str | None = None,
    options: dict[str, Any] | None = None,
    think: bool = False,
    stop_on_repeat: bool = False,
    out_meta: dict[str, Any] | None = None,
) -> Iterator[dict[str, str]]:
    """Stream chat from Ollama. Yields dicts: {"kind": "content"|"thinking", "text": "..."}.

    `stop_on_repeat`: abort the stream when the content tail locks into
    an exact-repetition loop (see `_loop_period`). Used by multi-response
    turns, where the model can spiral on a single phrase and burn the
    whole token budget — bounding it salvages the turn and the latency.

    `out_meta`: optional dict the caller passes in; on a clean finish it
    is populated with the final NDJSON line's `done_reason` (e.g.
    "stop" for a natural end, "length" when the generation was cut by the
    token/context budget) and `eval_count`. A side-channel rather than a
    yielded event so the many content/thinking consumers don't have to
    learn a new event kind. Left untouched on an error/abort path.

    For thinking-capable models (Gemma 4, gpt-oss, etc.), Ollama defaults to
    routing most generated tokens into a `thinking` field with a short final
    `content`. That's a 5-10x latency hit if the caller only wants the reply.
    Pass think=False (the default) to disable reasoning on these models; pass
    think=True to surface both fields so the UI can show the reasoning.
    """
    cfg = _cfg()
    target_model = model or cfg.get("model")
    if not target_model:
        yield {"kind": "content", "text": "[ollama error: no model configured. Set one in Settings.]"}
        return

    full_messages: list[dict[str, str]] = []
    if system:
        full_messages.append({"role": "system", "content": system})
    full_messages.extend(messages)

    merged_options: dict[str, Any] = {**(cfg.get("default_options") or {})}
    for k, v in (options or {}).items():
        if v is None or v == "":
            continue
        merged_options[k] = v

    # Default keep_alive to -1 so Ollama keeps the model loaded between turns
    # (matches SillyTavern's default and avoids re-loading on every message).
    keep_alive = cfg.get("keep_alive", -1)

    payload = {
        "model": target_model,
        "messages": full_messages,
        "stream": True,
        "options": merged_options,
        "keep_alive": keep_alive,
        "think": think,
    }

    timeout = cfg.get("request_timeout_seconds", 600)
    url = f"{_host()}/api/chat"
    sys_chars = sum(len(m["content"]) for m in full_messages if m["role"] == "system")
    user_chars = sum(len(m["content"]) for m in full_messages if m["role"] != "system")
    log.info(
        "POST %s model=%s msgs=%d sys=%dch hist=%dch num_ctx=%s keep_alive=%s think=%s",
        url, target_model, len(full_messages), sys_chars, user_chars,
        merged_options.get("num_ctx", "default"), keep_alive, think,
    )
    # Always show a short preview so it's obvious what we're sending.
    last_user = next((m["content"] for m in reversed(full_messages) if m["role"] != "system"), "")
    log.info("  last_user[%dch]: %s", len(last_user), last_user[:200].replace("\n", " ⏎ "))
    if log.isEnabledFor(logging.DEBUG):
        for m in full_messages:
            log.debug("  [%s] %s", m["role"], m["content"])

    import time as _time
    t_open = _time.monotonic()
    content_chars = 0
    thinking_chars = 0
    # Rolling tail of recent CONTENT for the repetition guard. Capped so
    # the O(period) scan per chunk stays cheap on long generations.
    repeat_tail = ""
    repeat_window = 600
    try:
        with requests.post(url, json=payload, stream=True, timeout=timeout) as r:
            log.info("  headers in %.2fs status=%s", _time.monotonic() - t_open, r.status_code)
            if not r.ok:
                body = ""
                try:
                    body = r.text[:500]
                except Exception:
                    pass
                yield {"kind": "content", "text": f"[ollama error: HTTP {r.status_code} — {body or r.reason}]"}
                return
            for raw in r.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                err = obj.get("error")
                if err:
                    yield {"kind": "content", "text": f"[ollama error: {err}]"}
                    return
                msg = obj.get("message") or {}
                thinking_chunk = msg.get("thinking") or ""
                content_chunk = msg.get("content") or ""
                if thinking_chunk:
                    thinking_chars += len(thinking_chunk)
                    yield {"kind": "thinking", "text": thinking_chunk}
                if content_chunk:
                    content_chars += len(content_chunk)
                    yield {"kind": "content", "text": content_chunk}
                    if stop_on_repeat:
                        repeat_tail = (repeat_tail + content_chunk)[-repeat_window:]
                        period = _loop_period(repeat_tail)
                        if period is not None:
                            log.info(
                                "  repetition loop detected (period=%dch) — aborting after %dch",
                                period, content_chars,
                            )
                            return
                if obj.get("done"):
                    log.info("  fields: content=%dch thinking=%dch", content_chars, thinking_chars)
                    _log_done_stats(obj)
                    if out_meta is not None:
                        out_meta["done_reason"] = obj.get("done_reason") or ""
                        out_meta["eval_count"] = obj.get("eval_count") or 0
                    return
    except requests.exceptions.ConnectionError:
        yield {"kind": "content", "text": f"[ollama error: cannot reach {_host()}. Is Ollama running?]"}
    except requests.exceptions.Timeout:
        yield {"kind": "content", "text": "[ollama error: request timed out]"}
    except Exception as e:
        yield {"kind": "content", "text": f"[ollama error: {e}]"}


def _log_done_stats(obj: dict[str, Any]) -> None:
    """Log the timing breakdown that Ollama returns on the final NDJSON line.

    Durations are in nanoseconds. Sample line:
      load=120ms prompt_eval=34tok/2.1s (16.0 t/s) gen=412tok/9.8s (42.1 t/s)
    """
    def ms(ns: Any) -> int:
        return int((ns or 0) / 1_000_000)
    def s(ns: Any) -> float:
        return (ns or 0) / 1_000_000_000

    load = ms(obj.get("load_duration"))
    pe_n = obj.get("prompt_eval_count") or 0
    pe_s = s(obj.get("prompt_eval_duration"))
    gen_n = obj.get("eval_count") or 0
    gen_s = s(obj.get("eval_duration"))
    total_s = s(obj.get("total_duration"))
    pe_rate = pe_n / pe_s if pe_s > 0 else 0
    gen_rate = gen_n / gen_s if gen_s > 0 else 0
    log.info(
        "done total=%.1fs load=%dms prompt_eval=%dtok/%.1fs (%.1f t/s) gen=%dtok/%.1fs (%.1f t/s)",
        total_s, load, pe_n, pe_s, pe_rate, gen_n, gen_s, gen_rate,
    )


def chat_sync(
    *,
    system: str,
    messages: list[dict[str, str]],
    model: str | None = None,
    options: dict[str, Any] | None = None,
    think: bool = False,
) -> str:
    """Collect a non-streaming chat response into one string.

    ``think`` defaults to False. Side calls (image-pack pick,
    auto-state detection, summarization) should always pass
    ``think=False`` explicitly — thinking-capable models otherwise
    spend tens of seconds on internal reasoning this caller has no
    use for, which the user perceives as a hang. The main streaming
    generate path uses ``chat_stream`` directly with the
    conversation's enable_thinking setting; this helper is for
    everything that should never think.
    """
    parts: list[str] = []
    for ev in chat_stream(
        system=system, messages=messages, model=model, options=options, think=think
    ):
        if ev["kind"] == "content":
            parts.append(ev["text"])
    return "".join(parts)
