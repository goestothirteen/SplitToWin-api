import logging
import os
import sys

from dotenv import load_dotenv
from flask import Flask

# Load .env before anything reads os.environ, including Config at import time.
load_dotenv()

from .config import Config  # noqa: E402


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, Config.LOG_LEVEL, logging.INFO))


def create_app() -> Flask:
    _configure_logging()

    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = Config.MAX_UPLOAD_BYTES

    # In production Caddy serves the UI and proxies /api on one hostname, so
    # there is no cross-origin request and CORS_ORIGINS stays empty. The old
    # `CORS(app)` reflected any Origin back, which turned the endpoint into an
    # open proxy anyone could point at to burn the API quota.
    if Config.CORS_ORIGINS:
        from flask_cors import CORS

        CORS(app, origins=Config.CORS_ORIGINS, methods=["GET", "POST"])

    from .routes import api

    app.register_blueprint(api)

    missing = Config.missing()
    if missing:
        # Warn loudly but still boot. A worker that dies here just restarts
        # forever and never reports why.
        app.logger.warning(
            "starting without %s - /parse-receipt will return 503", ", ".join(missing)
        )
    app.logger.info(
        "splittowin-api ready model=%s cors=%s",
        Config.GEMINI_MODEL,
        Config.CORS_ORIGINS or "same-origin",
    )
    return app
