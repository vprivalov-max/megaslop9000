"""Flask application object shared by every route module.

Route modules do `from sw.core import app` and keep their @app.route
decorators verbatim — endpoint names (and therefore url_for/OAuth
redirects) stay identical to the original monolithic app.py.
"""
import time
from pathlib import Path

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from sw.config import BASE

app = Flask(__name__,
            static_folder=str(BASE / 'static'),
            template_folder=str(BASE / 'templates'))

# Cache-bust token for /static/* — bumped automatically on every deploy/
# restart via the mtime of the most-recently-modified static file. Without
# this the browser holds onto stale app.js / style.css after a deploy and
# users keep seeing the previous bug forever («не помогло чет, так же всё»).
try:
    _static_dir = BASE / 'static'
    _static_mtime = max(
        (_p.stat().st_mtime for _p in _static_dir.glob('*') if _p.is_file()),
        default=0.0,
    )
    STATIC_VERSION = str(int(_static_mtime))
except Exception:
    STATIC_VERSION = str(int(time.time()))
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

# Behind Coolify/Traefik/Caddy reverse proxy — trust X-Forwarded-* headers so
# url_for(_external=True) builds correct https://<public-host>/auth/google/callback
# URLs. Without this Flask sees the inner http://app:8080 → Google rejects the
# OAuth start with `redirect_uri_mismatch`. Got accidentally deleted in a
# later refactor — restoring.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
