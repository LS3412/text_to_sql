"""
Logging configuration for the A2A application.
"""

import logging
import logging.handlers
import os
from pythonjsonlogger import jsonlogger
from config.settings import get_settings


def setup_logging() -> logging.Logger:
    """Configure logging for the application"""
    settings = get_settings()
    
    # Create logs directory if it does not exist
    os.makedirs("logs", exist_ok=True)
    
    # Create logger
    logger = logging.getLogger("a2a")
    logger.setLevel(getattr(logging, settings.app.log_level))
    
    # JSON formatter for structured logging
    json_formatter = jsonlogger.JsonFormatter(
        fmt="%(timestamp)s %(level)s %(name)s %(message)s",
        timestamp=True,
    )
    
    # Console handler with JSON format
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(json_formatter)
    logger.addHandler(console_handler)
    
    # File handler with rotation
    file_handler = logging.handlers.RotatingFileHandler(
        "logs/a2a.log",
        maxBytes=10485760,  # 10MB
        backupCount=5,
    )
    file_handler.setFormatter(json_formatter)
    logger.addHandler(file_handler)
    
    return logger


def get_logger(name: str) -> logging.Logger:
    """Get logger instance"""
    return logging.getLogger(f"a2a.{name}")
