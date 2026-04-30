"""Gunicorn config for production.

Single worker + threads, because:
  - JSON-file storage isn't shared between processes
  - The render-queue Semaphore is in-process; multiple workers would each have
    their own and ignore each other
  - 8 threads is plenty for 5–10 concurrent users
"""
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8080')}"
workers = 1
threads = int(os.environ.get('GUNICORN_THREADS', '8'))
worker_class = 'gthread'

# Renders can take up to 10 min; default 30s would kill them.
timeout = int(os.environ.get('GUNICORN_TIMEOUT', '600'))
graceful_timeout = 30
keepalive = 5

# Larger requests for image/video uploads.
limit_request_line = 8192
limit_request_field_size = 8192

accesslog = '-'
errorlog = '-'
loglevel = os.environ.get('LOG_LEVEL', 'info')
