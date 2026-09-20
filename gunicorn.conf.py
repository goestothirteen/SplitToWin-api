"""Gunicorn settings.

The old deploy ran gunicorn's defaults: one synchronous worker. A single
receipt parse — 5-20s of waiting on the model — blocked every other request
including the health check, so the backend looked dead exactly when it was
busiest. Threads fix that: the work is nearly all IO wait, so threads cost
almost nothing and let health checks answer during a parse.
"""

import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"

# ONE worker, deliberately. A receipt parse now outlives the request that
# started it and is polled for progress (services/jobs.py), and those jobs
# live in the worker's own memory — with two workers, half the polls would
# land on the process that has never heard of the job. Threads carry the
# concurrency instead, which suits an IO-bound handler on a 1 vCPU box.
workers = int(os.environ.get("WEB_CONCURRENCY", 1))
threads = int(os.environ.get("WEB_THREADS", 8))
worker_class = "gthread"

# Requests are all short now — the slow work happens on a background thread
# — so this only has to cover uploading and re-encoding a photo.
timeout = int(os.environ.get("WEB_TIMEOUT", 75))
graceful_timeout = 30
keepalive = 5

# Recycling is off: with a single worker, replacing it mid-parse would throw
# away every job in flight and the phones polling them would get a 404. The
# leak this used to guard against is now covered by restarting the container.
max_requests = int(os.environ.get("WEB_MAX_REQUESTS", 0))
max_requests_jitter = 50

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("LOG_LEVEL", "info").lower()
# Response time in ms (%(D)i is microseconds) so slow parses are visible.
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)i'

forwarded_allow_ips = "*"  # only Caddy can reach this container
