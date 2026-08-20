"""Thin adapters between HTTP and the existing modules.

Nothing in here reimplements pipeline logic. The two files that contain
real new machinery are run_manager.py (a sync-to-async bridge, because the
orchestrator is blocking and SSE is not) and metrics.py (which is itself
just another pipeline.ProgressCallback).
"""
