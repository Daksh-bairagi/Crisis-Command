import logging
import sys


class SafeStreamHandler(logging.StreamHandler):
    """Gracefully degrades when the terminal cannot encode Unicode log lines."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            stream = self.stream
            encoding = getattr(stream, "encoding", None) or "utf-8"
            safe_msg = msg.encode(encoding, errors="replace").decode(
                encoding, errors="replace"
            )
            stream.write(safe_msg + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)


def get_logger(name: str) -> logging.Logger:
    return _make_logger(name, sys.stdout)


def get_mcp_logger(name: str) -> logging.Logger:
    """Use this in ALL MCP servers. stdout is reserved for the MCP protocol."""
    return _make_logger(name, sys.stderr)


def _make_logger(name: str, stream) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = SafeStreamHandler(stream)
        formatter = logging.Formatter(
            "%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s",
            datefmt="%H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
    return logger
