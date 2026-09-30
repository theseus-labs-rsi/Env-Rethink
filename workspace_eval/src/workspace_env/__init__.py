"""Environment construction chains: workspace collection maps and work history.

Generation side
    ``workspace_collection_cover`` (full-coverage collection map) and
    ``event_synthesis`` (A/B/C work-history synthesis).  Both drive a real agent
    through an injected :class:`workspace_env.runner.RoleRunner`.

Delivery side
    ``server`` / ``runtime`` serve the generated artifacts to a task agent over
    MCP (``workspace_map`` / ``workspace_search`` / ``event_search``), staged
    from a fixture by ``manifest_staging``.
"""

__all__: list[str] = []
