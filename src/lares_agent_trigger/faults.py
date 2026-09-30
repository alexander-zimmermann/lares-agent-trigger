"""The fault sentences, read from the engine's own fault list.

lares mounts the engine's `faults.yaml` here unchanged. Only each entry's name
and sentence are read, so the rest of the engine's schema can move without
this package noticing, and a mail names the fault the way the list does.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def load_fault_sentences(path: Path) -> dict[str, str]:
    """Fault name to its sentence; a file that cannot be read is a ``ValueError``."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"{path}: cannot be read ({exc})") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: is not valid YAML ({exc})") from exc

    entries = raw.get("faults") if isinstance(raw, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{path}: has no `faults` list")
    return {
        str(entry["name"]): " ".join(str(entry["sentence"]).split())
        for entry in entries
        if isinstance(entry, dict) and "name" in entry and "sentence" in entry
    }


def first_clause(sentence: str) -> str:
    """What was measured: the sentence up to the dash before its reason, without the full stop.

    The list writes a fault as the measurement, a dash, and what it usually
    means; a sentence without the dash is all measurement.
    """
    return sentence.split(" — ", 1)[0].rstrip(" .")
