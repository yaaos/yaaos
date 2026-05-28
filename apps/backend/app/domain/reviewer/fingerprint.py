"""`FindingFingerprint` computation + anchor hashing.

Two raw findings with the same fingerprint = the same `Finding`. The recipe:

- `anchor_content_hash` — sha256 of the **line content** at `line_start..line_end`,
  whitespace-normalized (runs of whitespace collapsed to single space, trailing
  stripped, lines joined with `\n`).
- `body_gist_hash` — sha256 of normalized `rule_id + title` (lowercased,
  whitespace-collapsed). Body text varies between runs (model phrasing);
  rule + title is stable enough to dedupe.

Pure functions. No I/O. Identical inputs → identical hashes.
"""

from __future__ import annotations

import hashlib
import re

from app.domain.reviewer.types import FindingFingerprint

_WHITESPACE_RUN = re.compile(r"\s+")


def normalize_line(line: str) -> str:
    """Collapse whitespace runs, strip leading/trailing whitespace."""
    return _WHITESPACE_RUN.sub(" ", line).strip()


def hash_anchor_content(lines: list[str]) -> str:
    """sha256 of the anchored line block after whitespace normalization."""
    normalized = "\n".join(normalize_line(line) for line in lines)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def hash_body_gist(rule_id: str, title: str) -> str:
    """sha256 of `rule_id + title`, lowercased and whitespace-collapsed."""
    blob = normalize_line(f"{rule_id} {title}").lower()
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def compute_fingerprint(
    *,
    file_path: str,
    rule_id: str,
    anchored_lines: list[str],
    title: str,
) -> FindingFingerprint:
    """Build a `FindingFingerprint` from a raw finding + its anchored line content."""
    return FindingFingerprint(
        file_path=file_path,
        rule_id=rule_id,
        anchor_content_hash=hash_anchor_content(anchored_lines),
        body_gist_hash=hash_body_gist(rule_id, title),
    )
