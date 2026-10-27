"""Logging configuration for dna_pypsa."""

import logging
from pathlib import Path


def configure_logging(
    level: int = logging.INFO,
    format_str: str | None = None,
    datefmt: str | None = None,
    log_file: Path | None = None,
) -> logging.Logger:
    """Configure logging with consistent formatting across the package.

    Parameters
    ----------
    level : int
        Logging level (default: logging.INFO).
    format_str : str | None, optional
        Log format string. If None, uses default format.
    datefmt : str | None, optional
        Date format string. If None, uses default.
    log_file : Path | None, optional
        If provided, also write all log output to this file.

    Returns
    -------
    logging.Logger
        Configured root logger.

    """
    if format_str is None:
        format_str = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    if datefmt is None:
        datefmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(level=level, format=format_str, datefmt=datefmt)

    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(format_str, datefmt=datefmt))
        logging.getLogger().addHandler(file_handler)

    return logging.getLogger(__name__)
