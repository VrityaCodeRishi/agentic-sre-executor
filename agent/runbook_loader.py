# Used cursor code here to extract the runbook content and parse it
"""Load and parse runbook files."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class RunbookAction:
    """Represents a single actionable step in a runbook."""

    def __init__(self, action_id: str, data: Dict[str, Any]):
        self.action_id = action_id
        self.description = data.get("description", "")
        self.command = data.get("command", "")
        self.conditions = data.get("conditions", {})
        self.extra = {k: v for k, v in data.items() if k not in ["description", "command", "conditions"]}

    def render_command(self, context: Dict[str, str]) -> str:
        """Render command template with context variables."""
        cmd = self.command
        for key, value in context.items():
            cmd = cmd.replace(f"{{{key}}}", str(value))
        return cmd


class Runbook:
    """Represents a parsed runbook file."""

    def __init__(self, runbook_id: str, metadata: Dict[str, Any], content: str):
        self.runbook_id = runbook_id
        self.metadata = metadata
        self.content = content
        self.actions: List[RunbookAction] = []
        self._parse_actions()

    def _parse_actions(self) -> None:
        """Parse remediation actions from markdown content."""
        # Look for "## Remediation Actions" section
        actions_section = re.search(
            r"## Remediation Actions\s*\n(.*?)(?=\n## |\Z)", self.content, re.DOTALL
        )
        if not actions_section:
            return

        actions_text = actions_section.group(1)

        # Parse each action block (starts with ### Action N:)
        action_blocks = re.split(r"### Action \d+:", actions_text)
        for block in action_blocks[1:]:  # Skip first empty split
            # Extract action_id from - **action_id**: value
            action_id_match = re.search(r"- \*\*action_id\*\*:\s*`?(\w+)`?", block)
            if not action_id_match:
                continue

            action_id = action_id_match.group(1)

            # Extract all key-value pairs
            action_data: Dict[str, Any] = {}
            for line in block.split("\n"):
                line = line.strip()
                if not line:
                    continue
                # Match: - **key**: value or - **key**: `value`
                # Handle both single-line and multi-line values
                match = re.match(r"- \*\*(\w+)\*\*:\s*(?:`)?(.+?)(?:`)?$", line)
                if match:
                    key = match.group(1)
                    value = match.group(2).strip()
                    # Remove trailing backticks if present
                    value = value.rstrip("`").strip()
                    # Try to parse as dict/list if it looks like YAML
                    if value.startswith("{") or value.startswith("["):
                        try:
                            value = yaml.safe_load(value)
                        except Exception:
                            pass
                    action_data[key] = value

            if action_data:
                self.actions.append(RunbookAction(action_id, action_data))

    def get_action(self, action_id: str) -> Optional[RunbookAction]:
        """Get action by ID."""
        for action in self.actions:
            if action.action_id == action_id:
                return action
        return None


def load_runbook(runbook_id: str) -> Optional[Runbook]:
    """Load a runbook file by ID."""
    # Runbooks are in agent/runbooks/ directory
    runbooks_dir = Path(__file__).parent / "runbooks"
    runbook_file = runbooks_dir / f"{runbook_id}.md"

    if not runbook_file.exists():
        return None

    content = runbook_file.read_text()

    # Parse YAML frontmatter
    frontmatter_match = re.match(r"^---\n(.*?)\n---\n(.*)$", content, re.DOTALL)
    if not frontmatter_match:
        return None

    metadata = yaml.safe_load(frontmatter_match.group(1))
    markdown_content = frontmatter_match.group(2)

    return Runbook(runbook_id, metadata or {}, markdown_content)

