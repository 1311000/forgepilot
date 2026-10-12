# reporting-dashboard

The dashboard's own rolling-window helpers, split out of `metrics-stats` so the
dashboard can be released on its own.

    pytest -q

⚠️ The window arithmetic in `reporting/windows.py` was **copied** from
`metrics-stats` when the split happened, and the same off-by-one came with it: the
last full window of a series is dropped. That is why one ticket can cover both
repositories — see `examples/stat-fix/two-repos/`.
