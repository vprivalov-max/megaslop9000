"""Core routes: index page, translate, per-user config keys, auto-revise settings."""
from flask import jsonify, render_template, request

from sw.auth import (_get_user_avai_key, _get_user_reteller_key,
                     current_user_email)
from sw.config import (DEFAULT_AUTO_REVISE_INSTRUCTION, PRIMARY_USER_EMAIL,
                       _load_user_keys, _load_user_settings, _save_user_keys,
                       _save_user_settings)
from sw.core import app, STATIC_VERSION
from sw.llm import claude_ask

# ── Routes ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html', static_v=STATIC_VERSION)

@app.route('/api/translate', methods=['POST'])
def translate_text():
    text = (request.json or {}).get('text', '').strip()
    if not text:
        return jsonify({'error': 'no text'}), 400
    result = claude_ask(
        f'Переведи следующий текст на русский язык. Верни только перевод, без пояснений.\n\n{text}',
        system='You are a professional literary translator. Translate accurately, preserving tone, style, and screenplay formatting if present.'
    )
    return jsonify({'translation': result})


@app.route('/api/config', methods=['GET', 'POST'])
def config_route():
    """Per-user API key storage. Anthropic stays global (operator's billing).
    AVAI / Reteller are per-user — primary user falls back to global env so
    nothing breaks for the operator. Other users must enter their own keys.
    GET returns masked status (has_X flags) — never the actual key string."""
    email = current_user_email()
    if not email:
        return jsonify({'error': 'auth required'}), 401
    if request.method == 'POST':
        body = request.json or {}
        existing = _load_user_keys(email)
        # Only update fields that were sent (non-empty); empty string = clear
        if 'avai_key' in body:
            existing['avai_key'] = (body.get('avai_key') or '').strip()
        if 'reteller_key' in body:
            existing['reteller_key'] = (body.get('reteller_key') or '').strip()
        _save_user_keys(email, existing)
        return jsonify({'ok': True,
                        'has_avai_key': bool(existing['avai_key']) or email == PRIMARY_USER_EMAIL,
                        'has_reteller_key': bool(existing['reteller_key']) or email == PRIMARY_USER_EMAIL})
    keys = _load_user_keys(email)
    return jsonify({
        'email': email,
        'is_primary': email == PRIMARY_USER_EMAIL,
        # NEVER return raw keys to client — only presence flags
        'has_avai_key': bool(_get_user_avai_key()),
        'has_reteller_key': bool(_get_user_reteller_key()),
        'avai_key_masked': ('•' * 6 + (keys['avai_key'][-4:] if keys.get('avai_key') else '')) if keys.get('avai_key') else '',
        'reteller_key_masked': ('•' * 6 + (keys['reteller_key'][-4:] if keys.get('reteller_key') else '')) if keys.get('reteller_key') else '',
    })


@app.route('/api/user/auto-revise', methods=['GET', 'POST'])
def user_auto_revise_route():
    """Per-user storage for the «🎬 Автоматическая правка» instruction used by
    Turbo-mode pipelines after batch-compose. Mirrors colleague's
    AppSettings.autoReviseInstructionRu (src/shared/lib/auto-revise.ts)."""
    email = current_user_email()
    if not email:
        return jsonify({'error': 'auth required'}), 401
    if request.method == 'POST':
        body = request.json or {}
        cur = _load_user_settings(email)
        if 'auto_revise_enabled' in body:
            cur['auto_revise_enabled'] = bool(body.get('auto_revise_enabled'))
        if 'auto_revise_instruction' in body:
            text = (body.get('auto_revise_instruction') or '').strip()
            cur['auto_revise_instruction'] = text or DEFAULT_AUTO_REVISE_INSTRUCTION
        _save_user_settings(email, cur)
        return jsonify({'ok': True, **cur, 'default': DEFAULT_AUTO_REVISE_INSTRUCTION})
    cur = _load_user_settings(email)
    return jsonify({**cur, 'default': DEFAULT_AUTO_REVISE_INSTRUCTION})


