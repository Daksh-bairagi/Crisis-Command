import logging
import sys

def get_logger(name: str) -> logging.Logger:
    return _make_logger(name, sys.stdout)

def get_mcp_logger(name: str) -> logging.Logger:
    """Use this in ALL MCP servers — stdout is reserved for MCP protocol"""
    return _make_logger(name, sys.stderr)

def _make_logger(name: str, stream) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(stream)
        formatter = logging.Formatter(
            '%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s',
            datefmt='%H:%M:%S'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    return logger