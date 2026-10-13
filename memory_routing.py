"""Explicit context or local directory selection; no fallback across domains."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlparse

import redis
from redis.commands.search.query import Query

from memory_store import META_KEY, check_model, git, hold_wsl_open, index_exists, namespace_of, tag


@dataclass(frozen=True)
class Target:
    name: str
    redis_url: str
    domain: str
    read_only: bool
    project: str = ""


class Router:
    def __init__(self, path: Path):
        config = json.loads(path.read_text(encoding="utf-8"))
        if config.get("schema") != 1:
            raise ValueError("Expected routing schema 1")
        self.targets = {}
        endpoints = set()
        for name, value in config["targets"].items():
            url = urlparse(value["redis_url"])
            domain = value["domain"]
            project = value.get("project", "")
            read_only = value.get("read_only", True)
            if (url.scheme not in ("redis", "rediss") or not url.hostname
                    or url.path not in ("", "/0") or url.query or url.fragment
                    or domain not in ("work", "personal") or not isinstance(read_only, bool)
                    or (project and not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", project))):
                raise ValueError("Invalid routing target: " + name)
            if domain == "work" and (not read_only or not project):
                raise ValueError("A work target requires read_only=true and a fixed project")
            endpoint = (url.hostname.lower(), url.port or 6379)
            if endpoint in endpoints:
                raise ValueError("Targets must use separate Redis instances")
            endpoints.add(endpoint)
            self.targets[name] = Target(name, value["redis_url"], domain, read_only, project)
        if not self.targets:
            raise ValueError("At least one target is required")
        self.routes = []
        roots = set()
        for value in config.get("routes", []):
            root = Path(value["root"]).expanduser()
            if not root.is_absolute() or value["target"] not in self.targets:
                raise ValueError("Routes require absolute roots and a known target")
            root = root.resolve()
            project = value.get("project", "")
            target = self.targets[value["target"]]
            if ((not project and not target.project)
                    or (project and not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", project))
                    or (target.project and project and target.project != project)
                    or root in roots):
                raise ValueError("Ambiguous root or project in routing configuration")
            roots.add(root)
            self.routes.append((root, target, target.project or project))
        self.default = config.get("default_target")
        if self.default is not None:
            if len(self.targets) != 1 or self.default not in self.targets or not self.targets[self.default].project:
                raise ValueError("A default is allowed only for one fixed-project target")
        self.clients = {}
        self.lock = threading.Lock()

    def resolve(self, directory: str) -> tuple[Target, str]:
        path = Path(directory).resolve()
        candidates = [path]
        # A new linked worktree inherits the main checkout's explicit route.
        common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common:
            common_path = Path(common).resolve()
            candidates.append(common_path.parent if common_path.name == ".git" else common_path)
        matches = [(root, target, project) for root, target, project in self.routes
                   if any(p == root or p.is_relative_to(root) for p in candidates)]
        if matches:
            identities = {(target.name, project) for _, target, project in matches}
            if len(identities) != 1:
                raise ValueError("Conflicting directory/worktree routes; fix the local routing configuration")
            _, target, project = matches[0]
            return target, project
        if self.default:
            target = self.targets[self.default]
            return target, target.project
        raise ValueError("No memory route for this directory. Use list_memory_contexts and pass context "
                         "(plus project for personal memory), or add a mapping to MEMORY_ROUTES; no database was searched.")

    def select(self, context: str, project: str = "") -> tuple[Target, str]:
        """Select one configured database without consulting the filesystem or client roots."""
        context, project = context.strip(), project.strip().lower()
        if not context:
            raise ValueError("An explicit project requires context; use list_memory_contexts to choose one.")
        if context not in self.targets:
            raise ValueError("Unknown memory context. Available contexts: " + ", ".join(sorted(self.targets)))
        target = self.targets[context]
        if project and not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", project):
            raise ValueError("project must be 1-128 letters, digits, dots, underscores or hyphens.")
        if target.project:
            if project and project != target.project:
                raise ValueError(f'Context "{context}" is fixed to project "{target.project}".')
            return target, target.project
        if not project:
            raise ValueError(f'Context "{context}" requires an explicit project; use list_memory_contexts.')
        return target, project

    def contexts(self) -> list[dict]:
        """List project IDs and source names so clients can map topics to the right scope."""
        result = []
        for target in self.targets.values():
            client = self.connect(target)
            projects = {project for _, routed, project in self.routes if routed == target}
            namespace = namespace_of(client)
            has_index = index_exists(client, namespace.index)
            if target.project:
                projects = {target.project}
            elif has_index:
                projects.update(client.ft(namespace.index).tagvals("project"))
            sources = {}
            if has_index:
                # Fetch only routing metadata, never document bodies or embeddings. Multiple chunks
                # of one document collapse to one source; fixed targets reveal only their project.
                query = "@kind:{doc}"
                if target.project:
                    query += f" @project:{{{tag(target.project)}}}"
                offset = 0
                while True:
                    page = client.ft(namespace.index).search(
                        Query(query).return_fields("project", "source").paging(offset, 250))
                    for doc in page.docs:
                        if doc.project and doc.source:
                            projects.add(doc.project)
                            sources.setdefault(doc.project, set()).add(doc.source)
                    offset += len(page.docs)
                    if not page.docs or offset >= page.total:
                        break
            result.append({"context": target.name, "domain": target.domain,
                           "read_only": target.read_only, "project_required": not bool(target.project),
                           "projects": sorted(projects),
                           "resources": {project: sorted(sources.get(project, ())) for project in sorted(projects)}})
        return result

    def connect(self, target: Target):
        hold_wsl_open()
        with self.lock:
            if target.name not in self.clients:
                self.clients[target.name] = redis.Redis.from_url(target.redis_url, decode_responses=True,
                                                                socket_connect_timeout=3, socket_timeout=30)
            client = self.clients[target.name]
        client.ping()
        if client.hget(META_KEY, "corpus_domain") != target.domain:
            raise RuntimeError("Redis corpus domain does not match routing target " + target.name)
        check_model(client)
        return client


def configured_router() -> Router | None:
    path = os.environ.get("MEMORY_ROUTES")
    return Router(Path(path)) if path else None
