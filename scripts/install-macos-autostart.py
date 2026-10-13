"""Install the shared memory server as a macOS sign-in LaunchAgent (no project sync)."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

LABEL = "local.project-memory.mcp"
REPO = Path(__file__).resolve().parent.parent


def launch_agent(repo: Path, python: Path, docker: str, routes: Path,
                 containers: list[str], context: str, port: int, logs: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": ["/bin/bash", str(repo / "scripts/start-memory-macos.sh"),
                             docker, context, str(python), str(repo), str(port), *containers],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {
            "PATH": f"{Path(docker).parent}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "MEMORY_ROUTES": str(routes),
            "PYTHONUNBUFFERED": "1",
            # Provision the model before installing. Startup must also work offline.
            "HF_HUB_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(logs / "mcp-server.stdout.log"),
        "StandardErrorPath": str(logs / "mcp-server.stderr.log"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", type=Path, default=REPO / "memory_routes.local.json")
    parser.add_argument("--containers", type=Path, default=REPO / "redis-containers.local.json")
    parser.add_argument("--docker-context", default="colima")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dry-run", action="store_true", help="Print the plist without installing it")
    args = parser.parse_args()
    if sys.platform != "darwin":
        parser.error("This installer requires macOS")
    if not 1 <= args.port <= 65535:
        parser.error("Invalid HTTP port")
    docker = shutil.which("docker")
    python = Path(sys.prefix) / "bin/python"
    if not docker or not python.is_file() or sys.prefix == sys.base_prefix:
        parser.error("Install Docker and run this installer with .venv/bin/python")
    routes = args.routes.resolve(strict=True)
    containers = json.loads(args.containers.read_text(encoding="utf-8"))
    if (not isinstance(containers, list) or not containers
            or not all(isinstance(c, str) and c and not c.startswith("-") for c in containers)):
        parser.error("Containers file must be a nonempty array of existing container names")
    # Validate both routing and the existing containers before touching startup configuration.
    sys.path.insert(0, str(REPO))
    from memory_routing import Router
    Router(routes)
    subprocess.run([docker, "--context", args.docker_context, "container", "inspect", *containers],
                   check=True, stdout=subprocess.DEVNULL)
    logs = Path.home() / "Library/Logs/project-memory"
    data = plistlib.dumps(launch_agent(REPO, python, docker, routes, containers,
                                     args.docker_context, args.port, logs))
    if args.dry_run:
        print(data.decode(), end="")
        return
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    plist = agents / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if plist.exists():
        shutil.copy2(plist, plist.with_suffix(".plist.previous"))
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False)
    plist.write_bytes(data)
    subprocess.run(["plutil", "-lint", str(plist)], check=True)
    subprocess.run(["launchctl", "enable", f"{domain}/{LABEL}"], check=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True)
    print(f"Installed {plist}; starts now and at sign-in. Project synchronization is not installed.")
    print(f"Logs: {logs}; readiness: http://127.0.0.1:{args.port}/health")


if __name__ == "__main__":
    main()
