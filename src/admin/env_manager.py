"""Read / write the ``.env`` file for non-secret bot settings.

The writer preserves comments and blank lines while updating or appending
key=value pairs.  Values containing whitespace or special characters are
automatically quoted.
"""

from __future__ import annotations

import re
from pathlib import Path

# Regex for a KEY=VALUE line (handles optional quoting)
_KV_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*)="
    r"(?P<quote>['\"]?)(?P<value>.*?)(?P=quote)\s*$"
)


def read_env(env_path: Path) -> dict[str, str]:
    """Parse a ``.env`` file and return a ``{key: value}`` dict.

    Comments (``#``), blank lines, and ``export`` prefixes are handled
    gracefully.
    """
    result: dict[str, str] = {}
    if not env_path.is_file():
        return result

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip optional 'export ' prefix
        if stripped.startswith("export "):
            stripped = stripped[7:]
        m = _KV_RE.match(stripped)
        if m:
            result[m.group("key")] = m.group("value")
    return result


def write_env(env_path: Path, updates: dict[str, str]) -> None:
    """Update keys in the ``.env`` file, preserving structure.

    * Existing keys are updated in-place.
    * New keys are appended at the end.
    * Keys set to ``None`` in *updates* are removed.
    * Comments and blank lines are preserved.
    """
    remaining = dict(updates)
    lines: list[str] = []

    if env_path.is_file():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            # Detect export prefix
            check = stripped
            if check.startswith("export "):
                check = check[7:]

            m = _KV_RE.match(check)
            if m and m.group("key") in remaining:
                key = m.group("key")
                new_val = remaining.pop(key)
                if new_val is not None:
                    lines.append(f"{key}={_quote(new_val)}")
                # else: drop the line (delete)
            else:
                lines.append(line)

    # Append any new keys
    for key, val in remaining.items():
        if val is not None:
            lines.append(f"{key}={_quote(val)}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def remove_keys(env_path: Path, keys: set[str]) -> None:
    """Remove the specified keys from the ``.env`` file."""
    if not env_path.is_file():
        return

    lines: list[str] = []
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        check = stripped
        if check.startswith("export "):
            check = check[7:]
        m = _KV_RE.match(check)
        if m and m.group("key") in keys:
            continue
        lines.append(line)

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _quote(value: str) -> str:
    """Wrap *value* in double quotes if it contains special characters."""
    if not value:
        return '""'
    if any(c in value for c in (" ", "'", '"', "#", "$", "\n", "\t")):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value
