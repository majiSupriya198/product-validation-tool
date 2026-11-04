"""Logging with a redaction filter so tokens and hostnames never reach log files or the UI."""
from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS = [
    re.compile(r"dapi[0-9a-f]{20,}", re.I),                       # Databricks PATs
    re.compile(r"(?i)(token|access_token|password|secret)\s*[=:]\s*\S+"),
    re.compile(r"[A-Za-z0-9-]+\.cloud\.databricks\.com"),
    re.compile(r"[A-Za-z0-9-]+\.azuredatabricks\.net"),
    re.compile(r"/sql/1\.0/warehouses/\w+"),
]


def redact(text: str) -> str:
    out = str(text)
    for env in ("DBX_TOKEN", "DBX_HOST", "DBX_HTTP_PATH", "DBX_WAREHOUSE_ID"):
        val = os.environ.get(env)
        if val and len(val) > 3:
            out = out.replace(val, "***")
    for pat in _PATTERNS:
        out = pat.sub("***", out)
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


_configured = False


def get_logger(name: str = "pvt") -> logging.Logger:
    global _configured
    root = logging.getLogger("pvt")
    if not _configured:
        from config import LOG_DIR

        Path(LOG_DIR).mkdir(parents=True, exist_ok=True)
        root.setLevel(logging.INFO)
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        fh = RotatingFileHandler(Path(LOG_DIR) / "validation.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.addFilter(RedactingFilter())
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        sh.setLevel(logging.WARNING)
        sh.addFilter(RedactingFilter())
        root.addHandler(fh)
        root.addHandler(sh)
        root.propagate = False
        _configured = True
    return root if name == "pvt" else root.getChild(name)
