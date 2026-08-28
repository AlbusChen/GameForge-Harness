from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_HEADER = re.compile(r"^\[(?P<section>[A-Za-z0-9_]+)(?P<attributes>.*)]$")
_PROPERTY = re.compile(r"^(?P<key>[A-Za-z0-9_./:-]+)\s*=\s*(?P<value>.*)$")
_SUBRESOURCE_REFERENCE = re.compile(r'SubResource\(\s*"(?P<id>[^"\r\n]+)"\s*\)')
_NODE_PATHS_ATTRIBUTE = re.compile(
    r'\snode_paths=PackedStringArray\((?:\s*"(?:[^"\\]|\\.)*"\s*,?)*\)'
)
_MAXIMUM_OPERATIONS = 64
_MAXIMUM_VALUE_BYTES = 1024 * 1024
_RAW_HEADER_CONSTRUCTORS = frozenset(
    {"ExtResource", "NodePath", "PackedStringArray", "SubResource"}
)
_SECTION_ORDER = {
    "gd_scene": 0,
    "gd_resource": 0,
    "ext_resource": 10,
    "sub_resource": 20,
    "resource": 30,
    "node": 30,
    "connection": 40,
    "editable": 50,
}
GodotPrimitive = str | int | float | bool


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SectionSelector(StrictModel):
    section: str = Field(pattern=r"^[A-Za-z0-9_]+$")
    attributes: dict[str, str] = Field(default_factory=dict)


class ResourceMutation(StrictModel):
    kind: Literal["set_properties", "set_attributes", "append_section"]
    selector: SectionSelector | None = None
    section: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_]+$")
    attributes: dict[str, GodotPrimitive] = Field(default_factory=dict)
    properties: dict[str, GodotPrimitive] = Field(default_factory=dict)

    @model_validator(mode="after")
    def fields_match_kind(self) -> ResourceMutation:
        if self.kind == "set_properties":
            if (
                self.selector is None
                or self.section is not None
                or self.attributes
                or not self.properties
            ):
                raise ValueError(
                    "set_properties requires selector and non-empty properties, and cannot "
                    "set section/attributes"
                )
        elif self.kind == "set_attributes":
            if (
                self.selector is None
                or self.section is not None
                or not self.attributes
                or self.properties
            ):
                raise ValueError(
                    "set_attributes requires selector and non-empty attributes, and cannot "
                    "set section/properties"
                )
        elif self.selector is not None or not self.section:
            raise ValueError("append_section requires section and cannot set selector")
        _validate_mapping(self.attributes, label="attributes", primitives=True)
        _validate_mapping(self.properties, label="properties", primitives=True)
        return self


class ResourceMutationRequest(StrictModel):
    operations: tuple[ResourceMutation, ...] = Field(
        min_length=1,
        max_length=_MAXIMUM_OPERATIONS,
    )


@dataclass
class ResourceSection:
    section: str
    attributes: dict[str, str]
    header: str
    body: list[str] = field(default_factory=list)

    def identity(self) -> str:
        attributes = ",".join(f"{key}={value}" for key, value in self.attributes.items())
        return f"{self.section}[{attributes}]"

    def properties(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in self.body:
            matched = _PROPERTY.match(line)
            if matched:
                key = matched.group("key")
                if key in values:
                    raise ValueError(f"duplicate property in {self.identity()}: {key}")
                values[key] = matched.group("value")
        return values


@dataclass
class GodotResourceDocument:
    preamble: list[str]
    sections: list[ResourceSection]
    trailing_newline: bool = True

    @classmethod
    def parse(cls, content: str) -> GodotResourceDocument:
        if "\x00" in content:
            raise ValueError("Godot resource contains a NUL byte")
        lines = content.splitlines()
        preamble: list[str] = []
        sections: list[ResourceSection] = []
        current: ResourceSection | None = None
        for line in lines:
            matched = _HEADER.match(line.strip())
            if matched:
                current = ResourceSection(
                    section=matched.group("section"),
                    attributes=_parse_attributes(matched.group("attributes")),
                    header=line.strip(),
                )
                sections.append(current)
            elif current is None:
                preamble.append(line)
            else:
                current.body.append(line)
        if not sections:
            raise ValueError("file is not a recognized Godot text scene/resource")
        return cls(preamble, sections, content.endswith("\n"))

    def render(self) -> str:
        lines = [*self.preamble]
        for section in self.sections:
            lines.append(section.header)
            lines.extend(section.body)
        rendered = "\n".join(lines)
        return rendered + "\n" if self.trailing_newline else rendered

    def mutate(self, request: ResourceMutationRequest) -> list[dict[str, object]]:
        changes: list[dict[str, object]] = []
        for operation in request.operations:
            if operation.kind == "set_properties":
                assert operation.selector is not None
                selected = self.select(operation.selector)
                before = selected.properties()
                normalized_properties = {
                    key: _encode_property_value(value)
                    for key, value in operation.properties.items()
                }
                self._set_properties(selected, normalized_properties)
                after = selected.properties()
                changes.append(
                    {
                        "kind": operation.kind,
                        "object": selected.identity(),
                        "properties_before": {key: before.get(key) for key in operation.properties},
                        "properties_after": {key: after.get(key) for key in operation.properties},
                    }
                )
                continue

            if operation.kind == "set_attributes":
                assert operation.selector is not None
                selected = self.select(operation.selector)
                before = dict(selected.attributes)
                previous_header = selected.header
                selected.header = _update_header_attributes(
                    selected.header,
                    operation.attributes,
                )
                selected.attributes.update(
                    {
                        key: _normalize_header_value(value)
                        for key, value in operation.attributes.items()
                    }
                )
                if any(
                    section is not selected
                    and section.section == selected.section
                    and section.attributes == selected.attributes
                    for section in self.sections
                ):
                    selected.attributes = before
                    selected.header = previous_header
                    raise ValueError(f"resource section already exists: {selected.identity()}")
                changes.append(
                    {
                        "kind": operation.kind,
                        "object": selected.identity(),
                        "attributes_before": {key: before.get(key) for key in operation.attributes},
                        "attributes_after": {
                            key: selected.attributes.get(key) for key in operation.attributes
                        },
                    }
                )
                continue

            assert operation.section is not None
            normalized_attributes = {
                key: _normalize_header_value(value) for key, value in operation.attributes.items()
            }
            candidate = ResourceSection(
                section=operation.section,
                attributes=normalized_attributes,
                header=_format_header(operation.section, operation.attributes),
                body=[
                    f"{key} = {_encode_property_value(value)}"
                    for key, value in operation.properties.items()
                ],
            )
            if any(
                section.section == candidate.section and section.attributes == candidate.attributes
                for section in self.sections
            ):
                raise ValueError(f"resource section already exists: {candidate.identity()}")
            insertion_index = self._section_insertion_index(operation.section)
            if insertion_index > 0:
                previous = self.sections[insertion_index - 1]
                if previous.body and previous.body[-1] != "":
                    previous.body.append("")
            if insertion_index < len(self.sections) and (
                not candidate.body or candidate.body[-1] != ""
            ):
                candidate.body.append("")
            self.sections.insert(insertion_index, candidate)
            changes.append(
                {
                    "kind": operation.kind,
                    "object": candidate.identity(),
                    "properties_before": {},
                    "properties_after": candidate.properties(),
                }
            )
        self._sort_sub_resources()
        return changes

    def _section_insertion_index(self, section_name: str) -> int:
        rank = _SECTION_ORDER.get(section_name)
        if rank is None:
            return len(self.sections)
        insertion_index = len(self.sections)
        for index, section in enumerate(self.sections):
            existing_rank = _SECTION_ORDER.get(section.section)
            if existing_rank is not None and existing_rank > rank:
                insertion_index = index
                break
        return insertion_index

    def _sort_sub_resources(self) -> None:
        positions = [
            index
            for index, section in enumerate(self.sections)
            if section.section == "sub_resource"
        ]
        resources = [self.sections[index] for index in positions]
        by_id = {
            identifier: section
            for section in resources
            if (identifier := section.attributes.get("id")) is not None
        }
        dependencies = {
            id(section): {
                referenced
                for line in section.body
                for referenced in _SUBRESOURCE_REFERENCE.findall(line)
                if referenced in by_id
            }
            for section in resources
        }
        ordered: list[ResourceSection] = []
        emitted: set[str] = set()
        remaining = list(resources)
        while remaining:
            ready = next(
                (section for section in remaining if dependencies[id(section)] <= emitted),
                None,
            )
            if ready is None:
                raise ValueError("sub_resource dependency cycle is not allowed")
            remaining.remove(ready)
            ordered.append(ready)
            identifier = ready.attributes.get("id")
            if identifier is not None:
                emitted.add(identifier)
        for position, section in zip(positions, ordered, strict=True):
            self.sections[position] = section

    def select(self, selector: SectionSelector) -> ResourceSection:
        matches = [
            section
            for section in self.sections
            if section.section == selector.section
            and all(
                section.attributes.get(key) == value for key, value in selector.attributes.items()
            )
        ]
        if len(matches) != 1:
            reason = "missing" if not matches else "ambiguous"
            raise ValueError(
                f"resource selector is {reason}: "
                f"{selector.section} {json.dumps(selector.attributes, sort_keys=True)}"
            )
        return matches[0]

    def ensure_node_reference_metadata(
        self,
        section: ResourceSection,
        property_names: tuple[str, ...],
    ) -> dict[str, object] | None:
        """Declare serialized Node references without changing native NodePath fields."""
        if section not in self.sections or section.section != "node":
            raise ValueError("node-reference metadata requires a node section in this document")
        matched = _NODE_PATHS_ATTRIBUTE.search(section.header)
        existing = (
            tuple(json.loads(value) for value in re.findall(r'"(?:[^"\\]|\\.)*"', matched.group(0)))
            if matched is not None
            else ()
        )
        merged = tuple(dict.fromkeys((*existing, *property_names)))
        if merged == existing:
            return None
        encoded = (
            "PackedStringArray("
            + ", ".join(json.dumps(name, ensure_ascii=False) for name in merged)
            + ")"
        )
        if matched is not None:
            section.header = (
                section.header[: matched.start()]
                + f" node_paths={encoded}"
                + section.header[matched.end() :]
            )
        else:
            section.header = f"{section.header[:-1]} node_paths={encoded}]"
        section.attributes["node_paths"] = encoded
        return {
            "kind": "ensure_node_reference_metadata",
            "object": section.identity(),
            "node_paths_before": list(existing),
            "node_paths_after": list(merged),
        }

    @staticmethod
    def _set_properties(section: ResourceSection, updates: dict[str, str]) -> None:
        positions: dict[str, int] = {}
        for index, line in enumerate(section.body):
            matched = _PROPERTY.match(line)
            if not matched:
                continue
            key = matched.group("key")
            if key in positions:
                raise ValueError(f"duplicate property in {section.identity()}: {key}")
            positions[key] = index
        for key, value in updates.items():
            rendered = f"{key} = {value}"
            if key in positions:
                section.body[positions[key]] = rendered
            else:
                section.body.append(rendered)

    def snapshot(self, *, path: str, maximum_sections: int = 256) -> dict[str, object]:
        sections = []
        for section in self.sections[:maximum_sections]:
            sections.append(
                {
                    "section": section.section,
                    "attributes": section.attributes,
                    "properties": {
                        key: value[:2000] for key, value in list(section.properties().items())[:80]
                    },
                }
            )
        rendered = self.render()
        return {
            "path": path,
            "sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            "section_count": len(self.sections),
            "sections_truncated": len(self.sections) > maximum_sections,
            "sections": sections,
            "assertions": self.structural_assertions(),
        }

    def structural_assertions(self) -> list[dict[str, object]]:
        identities = [section.identity() for section in self.sections]
        unique = len(identities) == len(set(identities))
        node_sections = [section for section in self.sections if section.section == "node"]
        node_keys = {
            (section.attributes.get("name", ""), section.attributes.get("parent", ""))
            for section in node_sections
        }
        unique_nodes = len(node_keys) == len(node_sections)
        parent_paths = _node_paths(node_sections)
        missing_parents: list[str] = []
        for section, node_path in parent_paths:
            parent = section.attributes.get("parent")
            if parent is None or parent == ".":
                continue
            if parent not in {path for _, path in parent_paths}:
                missing_parents.append(f"{node_path}->{parent}")
        ordered_sections = [
            (section.section, _SECTION_ORDER[section.section])
            for section in self.sections
            if section.section in _SECTION_ORDER
        ]
        canonical_order = all(
            current[1] <= following[1]
            for current, following in zip(
                ordered_sections,
                ordered_sections[1:],
                strict=False,
            )
        )
        subresource_positions = {
            identifier: index
            for index, section in enumerate(self.sections)
            if section.section == "sub_resource"
            and (identifier := section.attributes.get("id")) is not None
        }
        ordered_subresource_dependencies = all(
            subresource_positions.get(referenced, -1) < index
            for index, section in enumerate(self.sections)
            if section.section == "sub_resource"
            for line in section.body
            for referenced in _SUBRESOURCE_REFERENCE.findall(line)
            if referenced in subresource_positions
        )
        return [
            {
                "id": "unique_section_identity",
                "passed": unique,
                "detail": "all section identities are unique" if unique else "duplicate sections",
            },
            {
                "id": "unique_node_identity",
                "passed": unique_nodes,
                "detail": "all node name/parent pairs are unique"
                if unique_nodes
                else "duplicate node name/parent pairs",
            },
            {
                "id": "node_parents_resolve",
                "passed": not missing_parents,
                "detail": "all declared node parents resolve"
                if not missing_parents
                else "; ".join(missing_parents[:20]),
            },
            {
                "id": "canonical_section_order",
                "passed": canonical_order,
                "detail": "resource declarations precede their consumers"
                if canonical_order
                else "resource sections are not in canonical Godot declaration order",
            },
            {
                "id": "subresource_dependencies_ordered",
                "passed": ordered_subresource_dependencies,
                "detail": "subresources precede every in-file consumer"
                if ordered_subresource_dependencies
                else "a subresource is declared after an in-file consumer",
            },
        ]


def _parse_attributes(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for key, encoded, _, _ in _attribute_tokens(raw):
        if key in values:
            raise ValueError(f"duplicate section attribute: {key}")
        values[key] = _decode_header_value(encoded)
    return values


def _attribute_tokens(raw: str) -> list[tuple[str, str, int, int]]:
    """Tokenize section attributes while retaining balanced Godot expressions."""
    tokens: list[tuple[str, str, int, int]] = []
    cursor = 0
    while cursor < len(raw):
        while cursor < len(raw) and raw[cursor].isspace():
            cursor += 1
        if cursor == len(raw):
            break
        key_start = cursor
        while cursor < len(raw) and (raw[cursor].isalnum() or raw[cursor] == "_"):
            cursor += 1
        key = raw[key_start:cursor]
        if not key or cursor >= len(raw) or raw[cursor] != "=":
            raise ValueError(f"invalid Godot section attributes near: {raw[key_start:]}")
        cursor += 1
        value_start = cursor
        if cursor >= len(raw):
            raise ValueError(f"missing Godot section attribute value: {key}")
        if raw[cursor] == '"':
            cursor = _quoted_token_end(raw, cursor)
        else:
            while cursor < len(raw) and not raw[cursor].isspace() and raw[cursor] not in "([{":
                cursor += 1
            if cursor < len(raw) and raw[cursor] in "([{":
                cursor = _balanced_token_end(raw, cursor)
        if cursor == value_start:
            raise ValueError(f"missing Godot section attribute value: {key}")
        if cursor < len(raw) and not raw[cursor].isspace():
            raise ValueError(f"invalid Godot section attribute value: {key}")
        tokens.append((key, raw[value_start:cursor], value_start, cursor))
    return tokens


def _quoted_token_end(raw: str, start: int) -> int:
    cursor = start + 1
    escaped = False
    while cursor < len(raw):
        character = raw[cursor]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == '"':
            return cursor + 1
        cursor += 1
    raise ValueError("unterminated quoted Godot section attribute")


def _balanced_token_end(raw: str, start: int) -> int:
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[raw[start]]]
    cursor = start + 1
    while cursor < len(raw):
        character = raw[cursor]
        if character == '"':
            cursor = _quoted_token_end(raw, cursor)
            continue
        if character in pairs:
            stack.append(pairs[character])
        elif character in pairs.values():
            if character != stack[-1]:
                raise ValueError("mismatched Godot section attribute delimiters")
            stack.pop()
            if not stack:
                return cursor + 1
        cursor += 1
    raise ValueError("unterminated Godot section attribute expression")


def _decode_header_value(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid quoted Godot header value: {value}") from error
        if not isinstance(decoded, str):
            raise ValueError("Godot header value must decode to a string")
        return decoded
    return value


def _format_header(section: str, attributes: dict[str, GodotPrimitive]) -> str:
    suffix = "".join(f" {key}={_encode_header_value(value)}" for key, value in attributes.items())
    return f"[{section}{suffix}]"


def _update_header_attributes(
    header: str,
    updates: dict[str, GodotPrimitive],
) -> str:
    rendered = header
    for key, value in updates.items():
        encoded = _encode_header_value(value)
        matched = _HEADER.match(rendered)
        if matched is None:
            raise ValueError(f"invalid Godot section header: {rendered}")
        raw = matched.group("attributes")
        token = next(
            (item for item in _attribute_tokens(raw) if item[0] == key),
            None,
        )
        if token is not None:
            _, _, start, end = token
            prefix_length = matched.start("attributes")
            rendered = rendered[: prefix_length + start] + encoded + rendered[prefix_length + end :]
        else:
            rendered = f"{rendered[:-1]} {key}={encoded}]"
    return rendered


def _normalize_header_value(value: GodotPrimitive) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _encode_header_value(value: GodotPrimitive) -> str:
    if isinstance(value, str):
        if _is_safe_header_expression(value):
            return value
        return json.dumps(value, ensure_ascii=False)
    return _encode_property_value(value)


def _is_safe_header_expression(value: str) -> bool:
    constructor = re.fullmatch(r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)\((?P<body>.*)\)", value)
    if constructor is not None and constructor.group("name") in _RAW_HEADER_CONSTRUCTORS:
        try:
            arguments = json.loads(f"[{constructor.group('body')}]")
        except json.JSONDecodeError:
            return False
        if not isinstance(arguments, list) or not all(
            isinstance(argument, str) for argument in arguments
        ):
            return False
        return constructor.group("name") == "PackedStringArray" or len(arguments) == 1
    if value.startswith("[") and value.endswith("]"):
        try:
            items = json.loads(value)
        except json.JSONDecodeError:
            return False
        return isinstance(items, list) and all(isinstance(item, str) for item in items)
    return False


def _encode_property_value(value: GodotPrimitive) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Godot numeric values must be finite")
    return str(value)


def _validate_mapping(
    values: dict[str, GodotPrimitive],
    *,
    label: str,
    primitives: bool = False,
) -> None:
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z0-9_./:-]+", key):
            raise ValueError(f"invalid {label} key: {key}")
        if not primitives and not isinstance(value, str):
            raise ValueError(f"{label} values must be strings")
        if not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"{label} values must be strings, numbers, or booleans")
        encoded = _encode_property_value(value)
        if "\n" in encoded or "\r" in encoded:
            if label != "properties":
                raise ValueError(f"{label} values must be single-line")
            encoded = " ".join(line.strip() for line in encoded.splitlines()).strip()
            if not encoded:
                raise ValueError("properties values cannot be empty after normalization")
            values[key] = encoded
        if len(encoded.encode("utf-8")) > _MAXIMUM_VALUE_BYTES:
            raise ValueError(f"{label} value exceeds {_MAXIMUM_VALUE_BYTES} bytes: {key}")


def _node_paths(
    sections: list[ResourceSection],
) -> list[tuple[ResourceSection, str]]:
    paths: list[tuple[ResourceSection, str]] = []
    for section in sections:
        name = section.attributes.get("name", "")
        parent = section.attributes.get("parent")
        if not name:
            paths.append((section, ""))
        elif parent is None or parent == ".":
            paths.append((section, name))
        else:
            paths.append((section, f"{parent}/{name}"))
    return paths


def resource_snapshot(content: str, *, path: str) -> dict[str, Any]:
    return GodotResourceDocument.parse(content).snapshot(path=path)
