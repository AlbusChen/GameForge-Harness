#!/usr/bin/env python3
"""Build a reproducible 50-task GameDevBench manifest without outcome access."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import zipfile
from collections import Counter
from pathlib import Path
from typing import Any

from experiments.run_unseen30_maturity import _runtime_digest

ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_ROOT = Path("/private/tmp/gamedevbench-e3868-pinned")
OUTPUT = ROOT / "benchmarks/gamedevbench-fresh50-v1.json"
COMMIT = "e3868bccbb88e86a3eb2d62154f1e9e0f3fd489a"
SEED = f"{COMMIT}:fresh50:official-v4:v1"
QUOTAS = {"2d": 25, "ui": 14, "3d": 4, "script_resource": 7}

_UI = re.compile(
    r"\b(ui|hud|dialog|menu|panel|scoreboard|overlay|balloon|canvaslayer|filedialog)\b",
    re.IGNORECASE,
)
_SCRIPT_RESOURCE = re.compile(
    r"\b(shader|tilemap|tile ?set|terrain|environment|worldenvironment|resource|particles?)\b|"
    r"\.(?:tres|gdshader)\b",
    re.IGNORECASE,
)
_THREE_D = re.compile(
    r"\b3d\b|\b(?:node3d|meshinstance3d|characterbody3d|raycast3d|camera3d|"
    r"collisionobject3d|area3d|marker3d)\b",
    re.IGNORECASE,
)


def _canonical(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _normalized(value: object) -> str:
    text = str(value or "").casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _public_config(archive: Path, task_id: str) -> dict[str, Any]:
    with zipfile.ZipFile(archive) as source:
        corrupt = source.testzip()
        if corrupt is not None:
            raise ValueError(f"corrupt public archive member: {task_id}:{corrupt}")
        expected = f"tasks/{task_id}/task_config.json"
        matches = [name for name in source.namelist() if name == expected]
        if len(matches) != 1:
            raise ValueError(f"public archive must contain one {expected}: {archive}")
        return json.loads(source.read(expected))


def _preflight_ground_truth(archive: Path, task_id: str) -> None:
    with zipfile.ZipFile(archive) as source:
        corrupt = source.testzip()
        if corrupt is not None:
            raise ValueError(f"corrupt ground-truth archive member: {task_id}:{corrupt}")


def _stratum(config: dict[str, Any]) -> str:
    text = json.dumps(
        {
            "name": config.get("name"),
            "instruction": config.get("instruction"),
            "metadata": config.get("metadata", {}),
        },
        sort_keys=True,
    )
    if _UI.search(text):
        return "ui"
    if _SCRIPT_RESOURCE.search(text):
        return "script_resource"
    if _THREE_D.search(text):
        return "3d"
    return "2d"


def _family(config: dict[str, Any]) -> str:
    metadata = config.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    variant = (
        metadata.get("variant_of")
        or metadata.get("source_variant_of")
        or metadata.get("base_task")
    )
    if variant:
        return f"variant_root:{_normalized(variant)}"

    video = _normalized(metadata.get("video_id"))
    if video and video not in {"unknown", "none", "n a", "na"}:
        return f"video:{video}"

    name = _normalized(config.get("name"))
    generic = {
        "a",
        "b",
        "centered",
        "codex",
        "create",
        "finish",
        "left",
        "right",
        "setup",
        "variant",
        "wide",
    }
    reduced = " ".join(token for token in name.split() if token not in generic)
    tutorial = _normalized(metadata.get("tutorial_source"))
    if tutorial:
        return f"source_name:{tutorial}:{reduced or name}"
    return f"name:{reduced or name}"


def _variant_root_task_id(config: dict[str, Any]) -> str | None:
    metadata = config.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    value = (
        metadata.get("variant_of")
        or metadata.get("source_variant_of")
        or metadata.get("base_task")
    )
    matched = re.fullmatch(r"task[ _-]?(\d{1,4})", _normalized(value))
    return f"task_{int(matched.group(1)):04d}" if matched else None


def _is_family_related(
    config: dict[str, Any], *, seen_ids: set[str], seen_families: set[str]
) -> bool:
    return _family(config) in seen_families or _variant_root_task_id(config) in seen_ids


def _seen_task_ids() -> set[str]:
    seen: set[str] = set()
    for path in sorted((ROOT / "benchmarks").glob("gamedevbench-*.json")):
        if path == OUTPUT:
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        tasks = payload.get("tasks")
        if not isinstance(tasks, list):
            continue
        for item in tasks:
            if isinstance(item, dict) and isinstance(item.get("task_id"), str):
                seen.add(item["task_id"])
    return seen


def _rank(task_id: str) -> str:
    return hashlib.sha256(f"{SEED}:{task_id}".encode()).hexdigest()


def main() -> int:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=BENCHMARK_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if head != COMMIT:
        raise RuntimeError(f"benchmark checkout is not pinned: {head}")

    seen_ids = _seen_task_ids()
    configs: dict[str, dict[str, Any]] = {}
    rejected: dict[str, str] = {}
    for archive in sorted((BENCHMARK_ROOT / "tasks").glob("task_*.zip")):
        task_id = archive.stem
        gt = BENCHMARK_ROOT / "tasks_gt" / archive.name
        try:
            config = _public_config(archive, task_id)
            _preflight_ground_truth(gt, task_id)
        except (OSError, ValueError, zipfile.BadZipFile) as error:
            rejected[task_id] = type(error).__name__
            continue
        configs[task_id] = config

    seen_instruction_hashes = {
        hashlib.sha256(str(configs[task]["instruction"]).encode()).hexdigest()
        for task in seen_ids
        if task in configs
    }
    seen_families = {_family(configs[task]) for task in seen_ids if task in configs}

    selected: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    selected_hashes: set[str] = set()
    selected_families: set[str] = set()
    ranked_task_ids = sorted(
        configs,
        key=lambda task_id: (
            _is_family_related(
                configs[task_id], seen_ids=seen_ids, seen_families=seen_families
            ),
            _rank(task_id),
        ),
    )
    for task_id in ranked_task_ids:
        if task_id in seen_ids:
            continue
        config = configs[task_id]
        instruction = str(config.get("instruction", ""))
        instruction_hash = hashlib.sha256(instruction.encode()).hexdigest()
        family = _family(config)
        stratum = _stratum(config)
        if counts[stratum] >= QUOTAS[stratum]:
            continue
        if instruction_hash in seen_instruction_hashes or instruction_hash in selected_hashes:
            continue
        if family in selected_families:
            continue
        family_related = _is_family_related(
            config, seen_ids=seen_ids, seen_families=seen_families
        )
        selected.append(
            {
                "task_id": task_id,
                "name": str(config.get("name", task_id)),
                "stratum": stratum,
                "selection_rank_sha256": _rank(task_id),
                "instruction_sha256": instruction_hash,
                "family_key": family,
                "novelty_tier": "family_related" if family_related else "family_new",
            }
        )
        counts[stratum] += 1
        selected_hashes.add(instruction_hash)
        selected_families.add(family)
        if counts == Counter(QUOTAS):
            break

    if counts != Counter(QUOTAS) or len(selected) != 50:
        available: dict[str, set[str]] = {stratum: set() for stratum in QUOTAS}
        for task_id, config in configs.items():
            instruction_hash = hashlib.sha256(
                str(config.get("instruction", "")).encode()
            ).hexdigest()
            if task_id not in seen_ids and instruction_hash not in seen_instruction_hashes:
                available[_stratum(config)].add(_family(config))
        raise RuntimeError(
            f"could not fill fresh50 quotas: selected={dict(counts)} "
            f"available_unique_families="
            f"{ {key: len(value) for key, value in available.items()} }"
        )
    runtime_digest, runtime_count = _runtime_digest(ROOT)
    payload = {
        "schema_version": 1,
        "id": "gamedevbench-fresh50-v1",
        "benchmark_commit": COMMIT,
        "purpose": (
            "Fresh paired Official Default versus programmable-open-global-diagnostic-v4 "
            "generalization evaluation. Selection is frozen before any outcome is observed."
        ),
        "selection_seed": SEED,
        "selection_method": (
            "Exclude every task in prior repository benchmark manifests and exact public "
            "instruction duplicates. Verify public and ground-truth archive CRCs without reading "
            "ground-truth contents. Prefer historically new normalized families, then use at most "
            "one task from each historically related family when necessary to fill fixed strata "
            "quotas; rank deterministically by SHA-256(seed:task_id)."
        ),
        "excluded_seen_task_ids": sorted(seen_ids),
        "mechanically_rejected_archives": rejected,
        "stratum_rules": {
            "ui": "public UI/HUD/dialog/menu/panel/control/container/label/button signals",
            "script_resource": "public shader/tile/resource/environment/particle signals",
            "3d": "public 3D or Godot 3D node-type signals",
            "2d": "all remaining tasks",
        },
        "quotas": QUOTAS,
        "experiment": {
            "conditions": ["official-default", "programmable-open-global-diagnostic-v4"],
            "runtime_protocol": "programmable-v1",
            "repetitions_per_task_condition": 1,
            "model": "gpt-5.6-sol",
            "reasoning_effort": "medium",
            "parallelism": 1,
            "primary_endpoint": "official evaluator PASS",
            "schedule": (
                "Each task's two attempts are adjacent. Even manifest positions run Official "
                "first; odd positions run v4 first, yielding 25 first positions per condition."
            ),
            "stop_rule": (
                "Stop before any model attempt if weekly subscription remaining is below 50%. "
                "No task replacement or task-specific Harness change is allowed."
            ),
        },
        "code_freeze": {
            "runtime_scope": ["gameforge/**", "pyproject.toml", "uv.lock"],
            "runtime_file_count": runtime_count,
            "runtime_tree_sha256": runtime_digest,
            "pre_freeze_verification": "183 pytest tests passed and Ruff passed on 2026-08-11.",
        },
        "tasks": selected,
    }
    encoded = _canonical(payload)
    protocol = ROOT / "runs/experiments/gamedevbench-fresh50-official-v4-v1/protocol-freeze.json"
    if OUTPUT.exists() and OUTPUT.read_bytes() != encoded and protocol.exists():
        raise RuntimeError(f"refusing to replace frozen manifest: {OUTPUT}")
    OUTPUT.write_bytes(encoded)
    print(OUTPUT)
    print(json.dumps(dict(counts), sort_keys=True))
    print(hashlib.sha256(encoded).hexdigest())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
