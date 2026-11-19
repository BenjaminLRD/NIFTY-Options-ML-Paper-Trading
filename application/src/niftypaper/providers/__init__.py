"""Read-only market-data providers (V6-0.5). Data in, nothing out: no order, position or account functionality exists here.

A provider is a callable `provider(now) -> Snapshot | None` (see niftypaper.dashboard.status). It may also expose a `last_error` string
explaining a None result. Providers never return synthetic data and never claim a feed is live without documented evidence.
"""
