"""GREEN LIGHT playbook routes — inspect and rebuild the distilled craft playbook
that trains the script/synopsis writer on proven (green-marked) series and
analyses of top short dramas."""
from flask import jsonify

from sw.core import app
from sw.greenlight import build_playbook, load_playbook
from sw.logging_utils import _log_event


@app.route('/api/greenlight/playbook', methods=['GET'])
def greenlight_playbook_get():
    """Return the current cached playbook + source metadata (or exists=False)."""
    pb = load_playbook()
    if not pb:
        return jsonify({'exists': False})
    return jsonify({
        'exists': True,
        'built_at': pb.get('built_at'),
        'sources': pb.get('sources', {}),
        'playbook': pb.get('playbook', ''),
    })


@app.route('/api/greenlight/playbook/rebuild', methods=['POST'])
def greenlight_playbook_rebuild():
    """Re-distill the playbook from GREEN LIGHT series + top-drama analyses."""
    try:
        rec = build_playbook(force=True)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        _log_event('WARN', 'greenlight_playbook_rebuild_fail', err=str(e)[:200])
        return jsonify({'error': f'rebuild failed: {e}'}), 500
    return jsonify({
        'ok': True,
        'built_at': rec.get('built_at'),
        'sources': rec.get('sources', {}),
        'playbook': rec.get('playbook', ''),
    })
