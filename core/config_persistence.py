"""Config persistence helpers extracted from Orchestrator."""

import logging
import os
from typing import Optional

from core.model_config import (
    model_spec_list_to_config_value,
    model_spec_map_to_config_value,
    model_spec_to_config_value,
    parse_model_spec,
    parse_model_spec_list,
    parse_model_spec_map,
)

log = logging.getLogger(__name__)


class ConfigPersistenceService:
    """Own runtime model config persistence details."""

    def __init__(self, orchestrator):
        self.orchestrator = orchestrator

    @property
    def config(self):
        return self.orchestrator.config

    def save_model_config(self):
        """Write model config changes back to config.yaml preserving formatting."""
        meta = self.config.get("_meta", {}) if isinstance(self.config, dict) else {}
        config_path = meta.get("config_path") if isinstance(meta, dict) else None
        if not config_path:
            config_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "config.yaml",
            )
        try:
            with open(config_path) as f:
                lines = f.readlines()

            oc = self.config["opencode"]
            explore = self.config.get("explore", {})
            new_lines = self.orchestrator._patch_yaml_lines(lines, oc, explore)

            with open(config_path, "w") as f:
                f.writelines(new_lines)
            log.info("Persisted model config to %s", config_path)
        except Exception as e:
            log.warning("Could not persist model config to %s: %s", config_path, e)

    @staticmethod
    def patch_yaml_lines(lines: list, oc: dict, explore: Optional[dict] = None) -> list:
        """Return a copy of lines with opencode model values patched in-place."""
        import re as _re

        explore = explore or {}
        planner_spec = parse_model_spec(oc.get("planner", oc.get("planner_model", "")))
        default_coder_spec = parse_model_spec(
            oc.get(
                "coder_default",
                oc.get("coder_model_default", oc.get("coder_model", "")),
            )
        )
        coder_specs = parse_model_spec_map(
            oc.get("coder_by_complexity", oc.get("coder_model_by_complexity", {}))
        )
        reviewer_specs = parse_model_spec_list(
            oc.get(
                "reviewers",
                oc.get("reviewer_models", [oc.get("reviewer_model", "")]),
            )
        )
        explorer_spec = parse_model_spec(
            explore.get("explorer", explore.get("explorer_model", ""))
        )
        map_spec = parse_model_spec(explore.get("map", explore.get("map_model", "")))

        scalar_values = {
            "planner_model": planner_spec.model,
            "coder_model_default": default_coder_spec.model,
            "coder_model_by_complexity": {
                level: spec.model for level, spec in coder_specs.items()
            },
            "reviewer_models": [spec.model for spec in reviewer_specs],
            "explorer_model": explorer_spec.model,
            "map_model": map_spec.model,
        }
        structured_values = {
            "planner": model_spec_to_config_value(planner_spec),
            "coder_default": model_spec_to_config_value(default_coder_spec),
            "coder_by_complexity": model_spec_map_to_config_value(coder_specs),
            "reviewers": model_spec_list_to_config_value(reviewer_specs),
            "explorer": model_spec_to_config_value(explorer_spec),
            "map": model_spec_to_config_value(map_spec),
        }
        result = list(lines)
        i = 0
        current_top_level_section = ""

        while i < len(result):
            line = result[i]
            stripped = line.rstrip()

            if stripped and not stripped.lstrip().startswith("#"):
                section_match = _re.match(
                    r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*$", stripped
                )
                if section_match:
                    current_top_level_section = section_match.group(1)

            m = _re.match(r"^(\s*(planner|planner_model)\s*:\s*)(.*)$", stripped)
            if m and current_top_level_section == "opencode":
                key = m.group(2)
                value = (
                    structured_values.get(key)
                    if key in structured_values
                    else scalar_values.get(key)
                )
                if value:
                    block_end = ConfigPersistenceService._find_block_end(result, i)
                    new_block = ConfigPersistenceService._render_section_entry(
                        key, value, len(line) - len(line.lstrip())
                    )
                    result[i:block_end] = new_block
                    i += len(new_block)
                    continue

            m = _re.match(
                r"^(\s*(coder_default|coder_model_default)\s*:\s*)(.*)$", stripped
            )
            if m and current_top_level_section == "opencode":
                key = m.group(2)
                value = (
                    structured_values.get(key)
                    if key in structured_values
                    else scalar_values.get(key)
                )
                if value:
                    block_end = ConfigPersistenceService._find_block_end(result, i)
                    new_block = ConfigPersistenceService._render_section_entry(
                        key, value, len(line) - len(line.lstrip())
                    )
                    result[i:block_end] = new_block
                    i += len(new_block)
                    continue

            m = _re.match(
                r"^(\s*(coder_by_complexity|coder_model_by_complexity)\s*:)", stripped
            )
            if m and current_top_level_section == "opencode":
                key = m.group(2)
                cmap = (
                    structured_values.get(key)
                    if key in structured_values
                    else scalar_values.get(key)
                )
                if cmap is None:
                    i += 1
                    continue
                indent = len(line) - len(line.lstrip())
                block_end = ConfigPersistenceService._find_block_end(result, i)
                new_block = ConfigPersistenceService._render_section_entry(
                    key, cmap, indent
                )
                result[i:block_end] = new_block
                i += len(new_block)
                continue

            m = _re.match(r"^(\s*(reviewers|reviewer_models)\s*:)", stripped)
            if m and current_top_level_section == "opencode":
                key = m.group(2)
                entries = (
                    structured_values.get(key)
                    if key in structured_values
                    else scalar_values.get(key)
                )
                if entries is None:
                    i += 1
                    continue
                indent = len(line) - len(line.lstrip())
                block_end = ConfigPersistenceService._find_block_end(result, i)
                new_block = ConfigPersistenceService._render_section_entry(
                    key, entries, indent
                )
                result[i:block_end] = new_block
                i += len(new_block)
                continue

            m = _re.match(r"^(\s*(explorer|explorer_model)\s*:\s*)(.*)$", stripped)
            if m and current_top_level_section == "explore":
                key = m.group(2)
                value = (
                    structured_values.get(key)
                    if key in structured_values
                    else scalar_values.get(key)
                )
                if value:
                    block_end = ConfigPersistenceService._find_block_end(result, i)
                    new_block = ConfigPersistenceService._render_section_entry(
                        key, value, len(line) - len(line.lstrip())
                    )
                    result[i:block_end] = new_block
                    i += len(new_block)
                    continue

            m = _re.match(r"^(\s*(map|map_model)\s*:\s*)(.*)$", stripped)
            if m and current_top_level_section == "explore":
                key = m.group(2)
                value = (
                    structured_values.get(key)
                    if key in structured_values
                    else scalar_values.get(key)
                )
                if value:
                    block_end = ConfigPersistenceService._find_block_end(result, i)
                    new_block = ConfigPersistenceService._render_section_entry(
                        key, value, len(line) - len(line.lstrip())
                    )
                    result[i:block_end] = new_block
                    i += len(new_block)
                    continue

            i += 1

        result = ConfigPersistenceService._ensure_section_entries(
            result,
            "opencode",
            {
                "planner": model_spec_to_config_value(planner_spec),
                "coder_default": model_spec_to_config_value(default_coder_spec),
                "coder_by_complexity": model_spec_map_to_config_value(coder_specs),
                "reviewers": model_spec_list_to_config_value(reviewer_specs),
            },
        )
        result = ConfigPersistenceService._ensure_section_entries(
            result,
            "explore",
            {
                "explorer": model_spec_to_config_value(explorer_spec),
                "map": model_spec_to_config_value(map_spec),
            },
        )
        return result

    @staticmethod
    def _yaml_inline_value(value) -> str:
        import yaml

        dumped = yaml.safe_dump(value, default_flow_style=True, sort_keys=False)
        cleaned = [line for line in dumped.splitlines() if line.strip() != "..."]
        return "\n".join(cleaned).strip()

    @staticmethod
    def _find_block_end(lines: list[str], start_index: int) -> int:
        line = lines[start_index]
        indent = len(line) - len(line.lstrip())
        block_end = start_index + 1
        header_stripped = line.strip()
        is_sequence_header = header_stripped.endswith(":")
        while block_end < len(lines):
            nxt = lines[block_end]
            if nxt.strip() == "" or nxt.strip().startswith("#"):
                block_end += 1
                continue
            nxt_indent = len(nxt) - len(nxt.lstrip())
            if nxt_indent < indent:
                break
            if nxt_indent == indent:
                nxt_stripped = nxt.lstrip()
                if not (is_sequence_header and nxt_stripped.startswith("-")):
                    break
            block_end += 1
        return block_end

    @staticmethod
    def _render_yaml_value(value, indent: int) -> list[str]:
        prefix = " " * indent
        lines: list[str] = []
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                if isinstance(child_value, (dict, list)):
                    lines.append(f"{prefix}{child_key}:\n")
                    lines.extend(
                        ConfigPersistenceService._render_yaml_value(
                            child_value, indent + 2
                        )
                    )
                else:
                    lines.append(
                        f"{prefix}{child_key}: {ConfigPersistenceService._yaml_inline_value(child_value)}\n"
                    )
            return lines
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    item_entries = list(item.items())
                    if not item_entries:
                        lines.append(f"{prefix}- {{}}\n")
                        continue
                    first_key, first_value = item_entries[0]
                    if isinstance(first_value, (dict, list)):
                        lines.append(f"{prefix}-\n")
                        lines.extend(
                            ConfigPersistenceService._render_yaml_value(item, indent + 2)
                        )
                        continue
                    lines.append(
                        f"{prefix}- {first_key}: {ConfigPersistenceService._yaml_inline_value(first_value)}\n"
                    )
                    nested_prefix = " " * (indent + 2)
                    for child_key, child_value in item_entries[1:]:
                        if isinstance(child_value, (dict, list)):
                            lines.append(f"{nested_prefix}{child_key}:\n")
                            lines.extend(
                                ConfigPersistenceService._render_yaml_value(
                                    child_value, indent + 4
                                )
                            )
                        else:
                            lines.append(
                                f"{nested_prefix}{child_key}: {ConfigPersistenceService._yaml_inline_value(child_value)}\n"
                            )
                    continue
                if isinstance(item, list):
                    lines.append(f"{prefix}-\n")
                    lines.extend(
                        ConfigPersistenceService._render_yaml_value(item, indent + 2)
                    )
                    continue
                lines.append(
                    f"{prefix}- {ConfigPersistenceService._yaml_inline_value(item)}\n"
                )
            return lines
        lines.append(f"{prefix}{ConfigPersistenceService._yaml_inline_value(value)}\n")
        return lines

    @staticmethod
    def _ensure_section_entries(lines: list, section_name: str, entries: dict) -> list:
        import re as _re

        section_start = None
        section_end = len(lines)
        seen = set()
        top_level_re = _re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*$")

        for idx, line in enumerate(lines):
            stripped = line.rstrip()
            if not stripped or stripped.lstrip().startswith("#"):
                continue
            top = top_level_re.match(stripped)
            if top:
                name = top.group(1)
                if section_start is not None and name != section_name:
                    section_end = idx
                    break
                if name == section_name and section_start is None:
                    section_start = idx
                continue
            if section_start is None:
                continue
            key_match = _re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", stripped)
            if key_match:
                seen.add(key_match.group(1))

        if section_start is None:
            return lines

        additions = []
        for key, value in entries.items():
            if not value or key in seen:
                continue
            additions.extend(
                ConfigPersistenceService._render_section_entry(key, value, 2)
            )

        if not additions:
            return lines
        return lines[:section_end] + additions + lines[section_end:]

    @staticmethod
    def _render_section_entry(key: str, value, indent: int) -> list[str]:
        prefix = " " * indent
        if isinstance(value, (dict, list)):
            return [f"{prefix}{key}:\n"] + ConfigPersistenceService._render_yaml_value(
                value, indent + 2
            )
        return [f"{prefix}{key}: {ConfigPersistenceService._yaml_inline_value(value)}\n"]
