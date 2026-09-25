"""Local lifecycle resources: bounded logs and a cross-process instance lock."""

import json
import logging
import logging.handlers
import os
from datetime import datetime, timezone

from .config import DigestError


class InstanceLock:
    def __init__(self, path):
        self.file = path.open("a+b")
        try:
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise DigestError("已有一个摘要插件实例正在运行，请等待旧实例退出。") from exc

    def close(self):
        self.file.close()


class Journal:
    def __init__(self, root, limits):
        directory = root / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("qq_group_digest.journal." + str(root))
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.handler = logging.handlers.RotatingFileHandler(
            directory / "digest.jsonl",
            maxBytes=limits.log_megabytes * 1024 * 1024,
            backupCount=limits.log_backups,
            encoding="utf-8",
        )
        self.handler.setFormatter(logging.Formatter("%(message)s"))
        self.logger.addHandler(self.handler)

    def record(self, event, **fields):
        # Call sites log only counts, identifiers, safe errors and exception TYPES.
        # Never log prompts, raw messages, provider responses, credentials or URLs.
        self.logger.info(
            json.dumps(
                {"time": datetime.now(timezone.utc).isoformat(), "event": event, **fields},
                ensure_ascii=False,
                default=str,
            )
        )

    def close(self):
        self.logger.removeHandler(self.handler)
        self.handler.close()
