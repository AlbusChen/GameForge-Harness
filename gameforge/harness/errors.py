from __future__ import annotations


class InfrastructureFailure(RuntimeError):
    """A host/runtime failure that must never be presented as a model repair task."""


__all__ = ["InfrastructureFailure"]
