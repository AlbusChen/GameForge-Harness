from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AcceptanceCondition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    given: str | None = None
    when: str
    assert_: str | list[str] = Field(alias="assert")

    @model_validator(mode="after")
    def assertions_must_not_be_empty(self) -> AcceptanceCondition:
        assertions = self.assert_ if isinstance(self.assert_, list) else [self.assert_]
        if not assertions or any(not item.strip() for item in assertions):
            raise ValueError("acceptance assertions must not be empty")
        return self

    @property
    def assertions(self) -> tuple[str, ...]:
        value = self.assert_ if isinstance(self.assert_, list) else [self.assert_]
        return tuple(value)
