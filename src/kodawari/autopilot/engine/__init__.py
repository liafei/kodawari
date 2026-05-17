"""Engine orchestration package for autopilot runtime."""

_EXPORTS = ("AutopilotConfig", "AutopilotEngine", "ExecutionPhase", "ExecutionPlan")

__all__ = list(_EXPORTS)


def __getattr__(name: str):
    if name in _EXPORTS:
        from .engine import AutopilotConfig, AutopilotEngine, ExecutionPhase, ExecutionPlan

        g = globals()
        g["AutopilotConfig"] = AutopilotConfig
        g["AutopilotEngine"] = AutopilotEngine
        g["ExecutionPhase"] = ExecutionPhase
        g["ExecutionPlan"] = ExecutionPlan
        return g[name]
    raise AttributeError(f"module 'kodawari.autopilot.engine' has no attribute {name!r}")
