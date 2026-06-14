"""Developer tooling for MyAnalysis (hot reload).

This package is intentionally excluded from hot-reload module discovery and from
`purge_project_modules` — it is the reloader itself and must survive a reload.
"""
