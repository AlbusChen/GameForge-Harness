from __future__ import annotations

import hashlib
import json
from typing import Protocol

from pydantic import Field

from gameforge.harness.base import StrictModel
from gameforge.harness.capabilities import CapabilityRegistry


class PluginManifest(StrictModel):
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    implementation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    capability_digests: dict[str, str] = Field(default_factory=dict)

    def digest(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class EnginePlugin(Protocol):
    def manifest(self) -> PluginManifest: ...

    def register(self, registry: CapabilityRegistry) -> None: ...
