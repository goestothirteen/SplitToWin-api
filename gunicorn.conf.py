"""Gunicorn settings.

The old deploy ran gunicorn's defaults: one synchronous worker. A single
receipt parse — 5-20s of waiting on the model — blocked every other request
including the health check, so the backend looked dead exactly when it was
busiest. Threads fix that: the work is nearly all IO wait, so threads cost
almost nothing and let health checks answer during a parse.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# 1 vCPU box shared with other containers. Two workers keeps a spare alive
# through a restart; four threads each is plenty for an IO-bound handler.
workers = int(os.environ.get("WEB_CONCURRENCY", 2))
threads = int(os.environ.get("WEB_THREADS", 4))
worker_class = "gthread"

# Must exceed GEMINI_TIMEOUT_S (default 45s) or gunicorn kills the worker
# mid-call and the client gets a 502 instead of a real error message.
timeout = int(os.environ.get("WEB_TIMEOUT", 75))
graceful_timeout = 30
keepalive = 5

# Recycle workers periodically so a slow leak can never accumulate on a 2 GB box.
max_requests = 500
max_requests_jitter = 50

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
# Response time in ms (%(D)i is microseconds) so slow parses are visible.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)i'

forwarded_allow_ips = "*"  # only Caddy can reach this container
