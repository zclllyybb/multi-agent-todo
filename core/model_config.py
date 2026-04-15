"""Unified model configuration parsing helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelSpec:
    model: str = ""
    variant: str = ""

    @property
    def is_set(self) -> bool:
        return bool(self.model)


def parse_model_spec(value: Any) -> ModelSpec:
    if isinstance(value, str):
        return ModelSpec(model=value.strip())
    if isinstance(value, dict):
        model = str(value.get("model", "")).strip()
        variant = str(value.get("variant", "")).strip()
        return ModelSpec(model=model, variant=variant)
    return ModelSpec()


def model_spec_to_config_value(spec: ModelSpec) -> Any:
    model = str(spec.model or "").strip()
    variant = str(spec.variant or "").strip()
    if not variant:
        return model
    return {
        "model": model,
        "variant": variant,
    }


def parse_model_spec_map(value: Any) -> dict[str, ModelSpec]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, ModelSpec] = {}
    for key, raw in value.items():
        spec = parse_model_spec(raw)
        if spec.is_set:
            out[str(key)] = spec
    return out


def model_spec_map_to_config_value(spec_map: dict[str, ModelSpec]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, spec in spec_map.items():
        if spec.is_set:
            out[str(key)] = model_spec_to_config_value(spec)
    return out


def parse_model_spec_list(value: Any) -> list[ModelSpec]:
    if not isinstance(value, list):
        return []
    out: list[ModelSpec] = []
    for raw in value:
        spec = parse_model_spec(raw)
        if spec.is_set:
            out.append(spec)
    return out


def model_spec_list_to_config_value(specs: list[ModelSpec]) -> list[Any]:
    out: list[Any] = []
    for spec in specs:
        if not spec.is_set:
            continue
        out.append(model_spec_to_config_value(spec))
    return out
