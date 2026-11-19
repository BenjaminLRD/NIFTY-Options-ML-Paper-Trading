"""Historical-data canonical layer (Phase 2). Made-up fixtures only: nothing here downloads, scrapes or reads real market data.

records   record kinds, strict validators, exclusion reason codes
manifest  SHA-256 manifests for raw files and integrity checks (byte-level only)
ingest    manifest-verified ingestion, duplicate/conflict policy, exclusion ledger
gate      replay clock and the available_at point-in-time view
bridge    the one narrow path from a validated quote snapshot to the paper engine

See docs/HISTORICAL_DATA_PHASE2.md.
"""
