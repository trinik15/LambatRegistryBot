"""Structured logging configuration (Phase 1.4).

Two output modes, selected by ``LOG_FORMAT``:

* ``console`` (default) — human-readable, coloured (when structlog is present)
  or a plain stdlib format (fallback). Best for local dev + Docker logs.
* ``json`` — one JSON object per log line, routed through structlog's stdlib
  integration so BOTH ``structlog.get_logger()`` calls and legacy
  ``logging.getLogger()`` calls emit the same JSON. Best for log aggregation
  (Loki, Datadog, CloudWatch).

Optional Sentry: when ``SENTRY_DSN`` is set AND ``sentry-sdk`` is installed,
we initialise it. The SDK patches logging to capture ERROR/CRITICAL as events.

This is called once from ``main.py`` at startup (after Config is imported so
``Config.LOG_FORMAT`` etc. are available). It is idempotent-ish: re-calling it
re-configures handlers, which is fine for tests.
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)


def _stdlib_fallback(level: int, log_file: str):
    """Plain stdlib logging when structlog isn't installed."""
    handlers: list[logging.Handler] = [
        logging.FileHandler(log_file),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=handlers,
        force=True,
    )


def _configure_structlog(level: int, log_file: str, json_output: bool):
    """Route both structlog + stdlib logging through a shared renderer."""
    import structlog
    from structlog.typing import Processor

    # Shared pre-chain: stuff added to EVERY log record (structlog or stdlib).
    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    structlog.configure(
        processors=shared_processors
        + [
            # Hand off to the stdlib formatter so both worlds share one renderer.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: Processor
    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        # ConsoleRenderer gives colour + key=value (readable in a terminal).
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    try:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as e:
        # If the log file can't be opened (e.g. read-only fs), don't crash —
        # stdout is still captured.
        logging.getLogger(__name__).warning(
            f"Could not open log file {log_file!r}: {e}. Logging to stdout only."
        )


def _init_sentry(dsn: str):
    """Initialise Sentry if the SDK is installed; warn (don't crash) if not."""
    try:
        import sentry_sdk
    except ImportError:
        logging.getLogger(__name__).warning(
            "SENTRY_DSN is set but `sentry-sdk` is not installed. "
            "Install with: pip install sentry-sdk  (or the [sentry] extra)."
        )
        return
    sentry_sdk.init(dsn=dsn, traces_sample_rate=0.0)
    logging.getLogger(__name__).info("Sentry initialised.")


def setup_logging():
    """Configure logging once at startup.

    Reads from Config (which reads env vars). Falls back to plain stdlib if
    structlog isn't available, so the bot still runs on a minimal install.
    """
    # Read directly from env to avoid a hard import-cycle: core.config imports
    # nothing from here, but importing Config triggers validation that may
    # raise before logging is set up. main.py calls setup_logging() AFTER a
    # successful Config import, so Config is safe to use then; but reading env
    # directly here keeps this function robust if called earlier in tests.
    log_format = os.getenv("LOG_FORMAT", "console").strip().lower()
    log_level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper()
    log_file = os.getenv("LOG_FILE", "lambat_bot.log")
    sentry_dsn = os.getenv("SENTRY_DSN", "").strip()

    level = getattr(logging, log_level_name, logging.INFO)

    try:
        _configure_structlog(level, log_file, json_output=(log_format == "json"))
    except ImportError:
        _stdlib_fallback(level, log_file)

    if sentry_dsn:
        _init_sentry(sentry_dsn)

    logging.getLogger(__name__).info(
        "Logging configured (format=%s, level=%s, file=%s, sentry=%s).",
        log_format,
        log_level_name,
        log_file,
        "on" if sentry_dsn else "off",
    )
