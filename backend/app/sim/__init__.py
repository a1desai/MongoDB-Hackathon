"""Simulation: scenario loading, in-memory World, vectorized physics, grid, baseline operator, metrics.

Modules: scenario (YAML), loader (World from Atlas), clock, weather, profiles (daily curves),
thermal (§7.3), behavior (§7.4-7.5), physics (world_step), grid (§7.6), baseline (§7.8),
metrics (§7.9 + map encoding), synthetic (test worlds), headless (CLI runner), aux_state.

Kept import-free on purpose: `app.runtime` imports `app.sim.world_state`, so importing heavy
submodules here would create cycles. Import submodules directly (`from app.sim import grid`).
"""
