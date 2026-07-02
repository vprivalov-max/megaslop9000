"""LLM clients: Anthropic Claude + OpenAI with lazy init and retry logic.

The module-level `_anthropic_client` / `_openai_client` are rebound at runtime
(lazy init) — they are private to this module; use claude_ask/llm_ask/etc.
"""
import random
import time

from sw.config import ANTHROPIC_KEY, OPENAI_KEY

_MODEL_ALIAS = {
    '':       'claude-sonnet-4-5',
    'sonnet': 'claude-sonnet-4-5',
    'haiku':  'claude-haiku-4-5',
}

_anthropic_client = None
_openai_client = None

# Allowed writer-model ids — frontend dropdown surfaces these. Anything else
# coming in body params falls back to the default. Keep this list tight so
# we don't accidentally route creative writing through Whisper or vision-
# only models.
WRITER_MODEL_DEFAULT = 'claude-sonnet-4-5'
WRITER_MODEL_WHITELIST = {'claude-sonnet-4-5', 'gpt-5.5'}


def _get_openai_client():
    """Lazy OpenAI client init. Returns None if no key — callers must check."""
    global _openai_client
    if _openai_client is None:
        if not OPENAI_KEY:
            return None
        import openai as _openai
        _openai_client = _openai.OpenAI(api_key=OPENAI_KEY)
    return _openai_client


def _openai_ask(prompt: str, system: str = '', model: str = 'gpt-5.5', max_tokens: int = 8192) -> str:
    """OpenAI Chat Completions wrapper. Mirrors `claude_ask` return shape
    (returns the assistant message content as a string). Retries up to 3
    times on 429/5xx with exponential backoff. Used by `llm_ask` when the
    selected model id starts with 'gpt'."""
    client = _get_openai_client()
    if client is None:
        raise RuntimeError('OPENAI_KEY не задан — пропиши openai_key в config.json или env')
    prompt_kb = len((system + prompt).encode('utf-8')) / 1024
    t0 = time.time()
    print(f'[openai_ask] {prompt_kb:.1f}KB → {model}', flush=True)
    messages = []
    if system:
        messages.append({'role': 'system', 'content': system})
    messages.append({'role': 'user', 'content': prompt})

    # Newer OpenAI models (o-series, gpt-5+) use max_completion_tokens instead of max_tokens.
    _model_lower = model.lower()
    _uses_completion_tokens = (
        _model_lower.startswith('o1') or _model_lower.startswith('o3') or
        _model_lower.startswith('o4') or _model_lower.startswith('gpt-5') or
        _model_lower.startswith('gpt5')
    )
    _tokens_kwarg = {'max_completion_tokens': max_tokens} if _uses_completion_tokens else {'max_tokens': max_tokens}

    last_err = None
    for attempt in range(4):   # 4 attempts total: 0 + 3 retries
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                **_tokens_kwarg,
            )
            text = (resp.choices[0].message.content or '').strip()
            print(f'[openai_ask] done in {time.time()-t0:.1f}s ({len(text)} chars)', flush=True)
            return text
        except Exception as e:
            last_err = e
            # Classify retriable: rate limit / 5xx / connection blips.
            msg = str(e).lower()
            transient = (
                'rate' in msg or '429' in msg or '5xx' in msg
                or '500' in msg or '502' in msg or '503' in msg or '529' in msg
                or 'overloaded' in msg or 'connection' in msg or 'timeout' in msg
            )
            if not transient or attempt == 3:
                print(f'[openai_ask] non-retriable / out of retries: {e}', flush=True)
                raise
            backoff = (2 ** attempt) + (0.3 * attempt)
            print(f'[openai_ask] attempt {attempt+1} failed ({type(e).__name__}: {str(e)[:120]}), retrying in {backoff:.1f}s', flush=True)
            time.sleep(backoff)
    raise last_err or RuntimeError('OpenAI call exhausted retries')


def _resolve_writer_model(body=None, series=None) -> str:
    """Pick the writer model for a creative-writing call: body param wins
    (per-call override from a UI chip), then series['writer_model'] (series-
    level default set at creation time), then global default."""
    body = body or {}
    requested = (body.get('model') or '').strip().lower()
    if requested:
        return requested if requested in WRITER_MODEL_WHITELIST else WRITER_MODEL_DEFAULT
    series_default = ((series or {}).get('writer_model') or '').strip().lower()
    if series_default in WRITER_MODEL_WHITELIST:
        return series_default
    return WRITER_MODEL_DEFAULT


def llm_ask(model: str, prompt: str, system: str = '', max_tokens: int = 8192) -> str:
    """Route a text-generation call to the appropriate provider based on the
    model id prefix. Used for CREATIVE-WRITING calls only (ideas + episode
    scripts) where the user gets a choice. Everything else stays on Claude.

      model='gpt-*'  → OpenAI Chat Completions
      anything else  → Anthropic Claude (claude_ask)
    """
    if not model:
        model = WRITER_MODEL_DEFAULT
    if model not in WRITER_MODEL_WHITELIST:
        print(f'[llm_ask] unknown writer model {model!r}, falling back to {WRITER_MODEL_DEFAULT}', flush=True)
        model = WRITER_MODEL_DEFAULT
    if model.startswith('gpt'):
        return _openai_ask(prompt, system=system, model=model, max_tokens=max_tokens)
    return claude_ask(prompt, system=system, model=model, max_tokens=max_tokens)


def _get_anthropic_client():
    global _anthropic_client
    if _anthropic_client is None:
        if not ANTHROPIC_KEY:
            raise RuntimeError('ANTHROPIC_KEY не задан — пропиши anthropic_key в config.json')
        import anthropic as _anthropic
        _anthropic_client = _anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    return _anthropic_client


def claude_ask(prompt: str, system: str = '', model: str = '', timeout: int = 1200, idle_timeout: int = 180, max_tokens: int = 8192) -> str:
    """Anthropic API call. Signature kept compatible with old CLI version —
    `timeout`/`idle_timeout` are accepted but ignored (SDK handles its own timeouts).
    Retries up to 3 times on 429 (rate-limit) / 529 (overloaded) with
    exponential backoff — critical for parallel range-gen where 3 concurrent
    compose calls used to fail 2/3 because Anthropic throttled the burst."""
    sdk_model = _MODEL_ALIAS.get(model, model) if model else 'claude-sonnet-4-5'
    prompt_kb = len((system + prompt).encode('utf-8')) / 1024
    t0 = time.time()
    print(f'[claude_ask] {prompt_kb:.1f}KB → {sdk_model}', flush=True)
    client = _get_anthropic_client()
    kwargs = {'model': sdk_model, 'max_tokens': max_tokens, 'messages': [{'role': 'user', 'content': prompt}]}
    if system:
        kwargs['system'] = system

    last_err = None
    for attempt in range(4):   # 4 attempts total: 0 + 3 retries
        try:
            if max_tokens >= 16000:
                text_parts = []
                stop_reason = None
                with client.messages.stream(**kwargs) as stream:
                    for chunk in stream.text_stream:
                        text_parts.append(chunk)
                    final = stream.get_final_message()
                    stop_reason = getattr(final, 'stop_reason', None)
                text = ''.join(text_parts).strip()
                out_kb = len(text.encode('utf-8')) / 1024
                print(f'[claude_ask] done(stream) in {time.time()-t0:.1f}s ({prompt_kb:.1f}KB→{out_kb:.1f}KB, stop={stop_reason}, try={attempt+1})', flush=True)
                return text
            msg = client.messages.create(**kwargs)
            text = ''.join(b.text for b in msg.content if getattr(b, 'type', '') == 'text').strip()
            out_kb = len(text.encode('utf-8')) / 1024
            print(f'[claude_ask] done in {time.time()-t0:.1f}s ({prompt_kb:.1f}KB→{out_kb:.1f}KB, stop={msg.stop_reason}, try={attempt+1})', flush=True)
            return text
        except Exception as e:
            last_err = e
            # Anthropic SDK exposes status_code on its API errors. 429 (rate
            # limit), 529 (overloaded), 500-503 (transient) all worth retrying.
            sc = getattr(e, 'status_code', None) or (e.response.status_code if hasattr(e, 'response') and hasattr(e.response, 'status_code') else None)
            err_name = e.__class__.__name__
            retryable = sc in (408, 429, 500, 502, 503, 504, 529) or err_name in (
                'RateLimitError', 'APIConnectionError', 'APITimeoutError',
                'InternalServerError', 'OverloadedError', 'APIStatusError',
            )
            if not retryable or attempt == 3:
                print(f'[claude_ask] FAIL after {attempt+1} tries ({time.time()-t0:.1f}s): {err_name}: {str(e)[:200]}', flush=True)
                raise
            # Exponential backoff with jitter: 2s, 6s, 14s
            delay = (2 ** attempt) * 2 + (random.random() * 1.5)
            print(f'[claude_ask] retry {attempt+1}/3 after {delay:.1f}s ({err_name}: {str(e)[:120]})', flush=True)
            time.sleep(delay)
    # Defensive: if loop exits without return/raise (shouldn't happen)
    if last_err:
        raise last_err
    raise RuntimeError('claude_ask: exhausted retries with no error captured')


def anthropic_ask(prompt: str, system: str = '', model: str = 'claude-haiku-4-5') -> str:
    """Direct Anthropic SDK call. Alias of claude_ask."""
    return claude_ask(prompt, system=system, model=model, max_tokens=4096)

def claude_ask_fast(prompt: str, system: str = '') -> str:
    """Haiku — quick tasks (extraction, classification)."""
    return claude_ask(prompt, system=system, model='haiku', max_tokens=4096)

def claude_ask_quality(prompt: str, system: str = '') -> str:
    """Sonnet — creative tasks (scripts, synopses, ideas)."""
    return claude_ask(prompt, system=system, model='sonnet', max_tokens=8192)

def claude_web_research(prompt: str, system: str = '', max_uses: int = 5,
                        model: str = 'sonnet', max_tokens: int = 4096) -> str:
    """Anthropic call with the native server-side web_search tool enabled.

    Web search is an Anthropic-only server tool: the search round-trips happen
    INSIDE a single messages.create call (no client-side tool loop), so we just
    read the text blocks of the final message — same as claude_ask. Used by the
    ideas pipeline (research stage) to ground 5-idea generation in what is
    actually trending in vertical short drama right now.

    Raises on failure (the caller is expected to fall back to a no-web
    model-knowledge digest). Retries transient errors like claude_ask."""
    sdk_model = _MODEL_ALIAS.get(model, model) if model else 'claude-sonnet-4-5'
    client = _get_anthropic_client()
    kwargs = {
        'model': sdk_model,
        'max_tokens': max_tokens,
        'messages': [{'role': 'user', 'content': prompt}],
        'tools': [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': max_uses}],
    }
    if system:
        kwargs['system'] = system
    t0 = time.time()
    last_err = None
    for attempt in range(4):
        try:
            msg = client.messages.create(**kwargs)
            text = ''.join(b.text for b in msg.content if getattr(b, 'type', '') == 'text').strip()
            searches = sum(1 for b in msg.content if getattr(b, 'type', '') == 'server_tool_use')
            print(f'[claude_web_research] done in {time.time()-t0:.1f}s '
                  f'({searches} searches, {len(text)} chars, try={attempt+1})', flush=True)
            return text
        except Exception as e:
            last_err = e
            sc = getattr(e, 'status_code', None) or (e.response.status_code if hasattr(e, 'response') and hasattr(e.response, 'status_code') else None)
            err_name = e.__class__.__name__
            retryable = sc in (408, 429, 500, 502, 503, 504, 529) or err_name in (
                'RateLimitError', 'APIConnectionError', 'APITimeoutError',
                'InternalServerError', 'OverloadedError', 'APIStatusError',
            )
            if not retryable or attempt == 3:
                print(f'[claude_web_research] FAIL after {attempt+1} tries ({time.time()-t0:.1f}s): {err_name}: {str(e)[:200]}', flush=True)
                raise
            delay = (2 ** attempt) * 2 + (random.random() * 1.5)
            print(f'[claude_web_research] retry {attempt+1}/3 after {delay:.1f}s ({err_name})', flush=True)
            time.sleep(delay)
    if last_err:
        raise last_err
    raise RuntimeError('claude_web_research: exhausted retries with no error captured')



def claude_ask_vision(prompt: str, image_urls, system: str = '',
                      model: str = 'haiku', max_tokens: int = 2048) -> str:
    """Multimodal Claude call — accepts list of HTTPS image URLs alongside prompt.
    Used to label continuity frames (who's visible / state / mise-en-scène)."""
    sdk_model = _MODEL_ALIAS.get(model, model) if model else 'claude-haiku-4-5'
    client = _get_anthropic_client()
    content = []
    for url in (image_urls or []):
        if not url or not url.lower().startswith(('http://', 'https://')):
            continue
        content.append({'type': 'image', 'source': {'type': 'url', 'url': url}})
    content.append({'type': 'text', 'text': prompt})
    n_imgs = len(content) - 1
    print(f'[claude_vision] {n_imgs} img(s) → {sdk_model}', flush=True)
    t0 = time.time()
    kwargs = {
        'model': sdk_model,
        'max_tokens': max_tokens,
        'messages': [{'role': 'user', 'content': content}],
    }
    if system:
        kwargs['system'] = system
    msg = client.messages.create(**kwargs)
    text = ''.join(b.text for b in msg.content if getattr(b, 'type', '') == 'text').strip()
    print(f'[claude_vision] done in {time.time()-t0:.1f}s', flush=True)
    return text


