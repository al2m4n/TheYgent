"""In-process logical-model registry — the management-plane store.

M1 keeps this in memory (no DB; Postgres-backed persistence is a later
control-plane concern). Maps a logical id -> its registered binding.
"""

from __future__ import annotations

from theygent_ir import ManagedBinding, ReachableBinding

Binding = ManagedBinding | ReachableBinding


class UnknownLogicalId(KeyError):
    """Raised when a logical id is not registered. On /v1/* this is what makes
    an engine name (e.g. "llamacpp") an invalid model value."""

    def __init__(self, logical_id: str) -> None:
        super().__init__(logical_id)
        self.logical_id = logical_id


class Registry:
    def __init__(self) -> None:
        self._models: dict[str, Binding] = {}

    def put(self, logical_id: str, binding: Binding) -> None:
        self._models[logical_id] = binding

    def get(self, logical_id: str) -> Binding | None:
        return self._models.get(logical_id)

    def require(self, logical_id: str) -> Binding:
        try:
            return self._models[logical_id]
        except KeyError:
            raise UnknownLogicalId(logical_id) from None

    def delete(self, logical_id: str) -> bool:
        return self._models.pop(logical_id, None) is not None

    def ids(self) -> list[str]:
        return list(self._models)

    def items(self) -> list[tuple[str, Binding]]:
        return list(self._models.items())
