# Node is here because the claude_code provider shells out to the Claude Code
# CLI. Both stages are bookworm-based, so the glibc the Node binary was linked
# against matches and it can simply be copied across.
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# --- Node + the Claude Code CLI -------------------------------------------
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && ln -sf /usr/local/lib/node_modules/@anthropic-ai/claude-code/cli.js /usr/local/bin/claude \
    && chmod +x /usr/local/bin/claude

WORKDIR /app

# Dependencies first so code edits don't invalidate the layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY gunicorn.conf.py run.py ./

# uid 1001 deliberately: it matches the host account whose Claude Code
# subscription login is mounted in, so the CLI can read its credentials and
# write refreshed tokens back. Compose sets the same uid.
RUN useradd --create-home --uid 1001 --shell /bin/bash appuser \
    && chown -R appuser /app
USER appuser
ENV HOME=/home/appuser

EXPOSE 8000
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:create_app()"]
