"""Remove only this repository's retired memory hooks, retaining other client settings."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid


def remove_hooks(data: dict, script: Path) -> tuple[dict, int]:
    # Copy before editing so preview and callers retain the exact original structure.
    data = json.loads(json.dumps(data))
    target = str(script).replace("\\", "/").lower()
    removed = 0
    hooks = data.get("hooks", {})
    for event, groups in list(hooks.items()):
        kept = []
        for group in groups:
            members = []
            for hook in group.get("hooks", []):
                command = str(hook.get("command", "")).replace("\\", "/").lower()
                if target in command:
                    removed += 1
                else:
                    members.append(hook)
            if members or "hooks" not in group:
                kept.append({**group, "hooks": members} if "hooks" in group else group)
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if "hooks" in data and not hooks:
        del data["hooks"]
    return data, removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="back up and update the client configurations")
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
    parser.add_argument("--claude-home", type=Path, default=Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))))
    args = parser.parse_args()
    script = Path(__file__).resolve().parents[1] / "hooks" / "stop_hook.py"
    for path in (args.codex_home / "hooks.json", args.claude_home / "settings.json"):
        if not path.exists():
            continue
        original = path.read_bytes()
        data, count = remove_hooks(json.loads(original.decode("utf-8-sig")), script)
        print(f"{path}: {count} retired memory hooks")
        if args.apply and count:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            backup = path.with_name(path.name + f".before-background-sync-{stamp}-{uuid.uuid4().hex[:6]}.bak")
            backup.write_bytes(original)
            temporary = path.with_name(path.name + ".sync.tmp")
            temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            temporary.replace(path)
            print(f"  backed up to {backup}")


if __name__ == "__main__":
    main()
