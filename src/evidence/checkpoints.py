"""Partial extraction recovery. A checkpoint is never a completed audit artifact."""

import copy
import json
import logging

from src.evidence.text import digest_json
from src.utils.safe_io import atomic_write_json

logger = logging.getLogger(__name__)


class ExtractionCheckpoint:
    def __init__(self, path, identity, recorder):
        self.path = path
        self.recorder = recorder
        self.fingerprint = digest_json(identity)
        self.completed = {}
        self.reused = 0
        if path is None or not path.exists():
            return
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            checksum = value.pop("checksum")
            if checksum != digest_json(value) or value["fingerprint"] != self.fingerprint:
                raise ValueError("Checkpoint inputs or checksum changed")
            if value["version"] != 1 or not isinstance(value["completed"], dict):
                raise ValueError("Checkpoint schema changed")
            records = value["provider_runs"]
            if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
                raise ValueError("Checkpoint call records are invalid")
            self.completed = value["completed"]
            if self.recorder is not None:
                known = {r.get("id") for r in self.recorder.records}
                self.recorder.records.extend(r for r in records if r.get("id") not in known)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            logger.warning("Saved extraction checkpoint could not be reused; reading the PDF again.")

    def get(self, name):
        if name not in self.completed:
            return None
        self.reused += 1
        logger.info("Resuming extraction: reusing completed %s request.", name)
        return copy.deepcopy(self.completed[name])

    def save(self, name=None, value=None):
        if name is not None:
            self.completed[name] = copy.deepcopy(value)
        if self.path is None:
            return
        value = {"version": 1, "fingerprint": self.fingerprint,
                 "completed": self.completed,
                 "provider_runs": self.recorder.records if self.recorder is not None else []}
        atomic_write_json(self.path, {**value, "checksum": digest_json(value)})

    def finish(self):
        if self.path is not None:
            self.path.unlink(missing_ok=True)
