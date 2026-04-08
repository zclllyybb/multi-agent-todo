"""Config persistence helpers extracted from Orchestrator."""

import logging
import os
from typing import Optional

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

            m = _re.match(r"^(\s*planner_model\s*:\s*)(.*)$", stripped)
            if m and current_top_level_section == "opencode" and "planner_model" in oc:
                result[i] = m.group(1) + oc["planner_model"] + "\n"
                i += 1
                continue

            m = _re.match(r"^(\s*coder_model_default\s*:\s*)(.*)$", stripped)
            if (
                m
                and current_top_level_section == "opencode"
                and "coder_model_default" in oc
            ):
                result[i] = m.group(1) + oc["coder_model_default"] + "\n"
                i += 1
                continue

            m = _re.match(r"^(\s*coder_model_by_complexity\s*:)", stripped)
            if (
                m
                and current_top_level_section == "opencode"
                and "coder_model_by_complexity" in oc
            ):
                indent = len(line) - len(line.lstrip())
                block_end = i + 1
                while block_end < len(result):
                    nxt = result[block_end]
                    if nxt.strip() == "" or nxt.strip().startswith("#"):
                        block_end += 1
                        continue
                    nxt_indent = len(nxt) - len(nxt.lstrip())
                    if nxt_indent <= indent:
                        break
                    block_end += 1

                cmap = oc["coder_model_by_complexity"]
                new_block = [result[i]]
                child_indent = " " * (indent + 4)
                for j in range(i + 1, block_end):
                    orig = result[j]
                    cm = _re.match(r"^(\s*)([a-zA-Z_]+)(\s*:\s*)(.*)$", orig.rstrip())
                    if cm and cm.group(2) in cmap:
                        new_block.append(
                            cm.group(1)
                            + cm.group(2)
                            + cm.group(3)
                            + cmap[cm.group(2)]
                            + "\n"
                        )
                    else:
                        new_block.append(orig)

                existing_levels = set()
                for j in range(i + 1, block_end):
                    cm = _re.match(r"^\s*([a-zA-Z_]+)\s*:", result[j].rstrip())
                    if cm:
                        existing_levels.add(cm.group(1))
                for level, model in cmap.items():
                    if level not in existing_levels:
                        new_block.append(f"{child_indent}{level}: {model}\n")
                result[i:block_end] = new_block
                i += len(new_block)
                continue

            m = _re.match(r"^(\s*reviewer_models\s*:)", stripped)
            if (
                m
                and current_top_level_section == "opencode"
                and "reviewer_models" in oc
            ):
                indent = len(line) - len(line.lstrip())
                block_end = i + 1
                while block_end < len(result):
                    nxt = result[block_end]
                    if nxt.strip() == "" or nxt.strip().startswith("#"):
                        block_end += 1
                        continue
                    nxt_indent = len(nxt) - len(nxt.lstrip())
                    if nxt_indent < indent:
                        break
                    if nxt_indent == indent and not nxt.lstrip().startswith("-"):
                        break
                    block_end += 1
                child_indent = " " * (indent + 2)
                new_block = [result[i]]
                for model in oc["reviewer_models"]:
                    new_block.append(f"{child_indent}- {model}\n")
                result[i:block_end] = new_block
                i += len(new_block)
                continue

            m = _re.match(r"^(\s*explorer_model\s*:\s*)(.*)$", stripped)
            if (
                m
                and current_top_level_section == "explore"
                and "explorer_model" in explore
            ):
                result[i] = m.group(1) + explore["explorer_model"] + "\n"
                i += 1
                continue

            m = _re.match(r"^(\s*map_model\s*:\s*)(.*)$", stripped)
            if m and current_top_level_section == "explore" and "map_model" in explore:
                result[i] = m.group(1) + explore["map_model"] + "\n"
                i += 1
                continue

            i += 1

        return result
