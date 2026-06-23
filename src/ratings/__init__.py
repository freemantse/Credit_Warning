"""
Ratings data workstream (Stage 1): ingest real agency rating history (Moody's /
Fitch / S&P, via file-based LSEG drops), crosswalk it to CIK, and build the
point-in-time ML label table.

Modules:
  scale.py     — notation ↔ rating_index normalization (the 0=AAA…21=D axis).
  ingest.py    — load the LSEG CSVs, pattern-detect columns, extract change-events.
  crosswalk.py — resolve PermID/RIC → CIK (reusing the SEC EDGAR bridges).
  labels.py    — absorbing-state as-of resolution + lookahead-free rating_labels.

Everything here is file/data-driven and unit-tested against mock CSVs, so the real
LSEG drop is a data event, not a code change. Persistence lives in src/store.py.
"""
