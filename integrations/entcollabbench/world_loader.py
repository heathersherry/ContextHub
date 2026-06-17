"""Load EntCollabBench world metadata into ContextHub contexts.

The loader accepts a small, duck-typed world object so Task 9 can pass either
EntCollabBench runtime objects or dataset-derived fixtures without importing
the benchmark at module import time.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
import json
import uuid
from typing import Any

from contexthub.db.repository import PgRepository, ScopedRepo

from integrations.entcollabbench import mapping


@dataclass
class LoadedWorld:
    """Metadata returned after loading an EntCollabBench world."""

    loaded_uris: set[str] = field(default_factory=set)
    role_to_owner_space: dict[str, str] = field(default_factory=dict)
    object_id_to_uri: dict[str, str] = field(default_factory=dict)

    def object_exists(self, object_id: str) -> bool:
        return self.object_uri(object_id) in self.loaded_uris

    def object_uri(self, object_id: str) -> str:
        raw = str(object_id or "").strip()
        if raw.startswith("ctx://"):
            return raw.split("@v", 1)[0]
        return self.object_id_to_uri.get(raw, mapping.object_uri(raw))


class WorldLoader:
    """把 EntCollabBench world 装载为 ContextHub governed context."""

    def __init__(self, repo: PgRepository, account_id: str):
        self._repo = repo
        self._account_id = account_id

    async def load(self, world) -> LoadedWorld:
        loaded = LoadedWorld()
        async with self._repo.session(self._account_id) as db:
            uri_to_id: dict[str, uuid.UUID] = {}

            for role in _iter_records(world, "roles"):
                role_name = _record_name(role, "role", "name", "agent")
                owner_space = _record_value(role, "owner_space") or mapping.role_to_owner_space(role_name)
                uri = mapping.role_uri(role_name)
                context_id = await _upsert_context(
                    db,
                    uri=uri,
                    context_type="resource",
                    scope="team",
                    owner_space=owner_space,
                    version=_record_version(role),
                    tags=["entcollab", "role"],
                    content=_content("role", role_name, role),
                )
                loaded.loaded_uris.add(uri)
                loaded.role_to_owner_space[role_name] = owner_space
                uri_to_id[uri] = context_id

            for tool in _iter_records(world, "tool_schemas", "tools"):
                tool_name = _record_name(tool, "tool_name", "name", "server", "mcp_server_name")
                uri = mapping.tool_schema_uri(tool_name)
                context_id = await _upsert_context(
                    db,
                    uri=uri,
                    context_type="resource",
                    scope="datalake",
                    owner_space=None,
                    version=_record_version(tool),
                    tags=["entcollab", "tool_schema"],
                    content=_content("tool_schema", tool_name, tool),
                )
                loaded.loaded_uris.add(uri)
                uri_to_id[uri] = context_id

            for policy in _iter_records(world, "policies", "approval_policies"):
                policy_id = _record_name(policy, "policy_id", "id", "name")
                owner_space = _record_value(policy, "owner_space") or "approval_center"
                uri = mapping.policy_uri(policy_id)
                context_id = await _upsert_context(
                    db,
                    uri=uri,
                    context_type="resource",
                    scope="team",
                    owner_space=owner_space,
                    version=_record_version(policy),
                    tags=["entcollab", "policy"],
                    content=_content("policy", policy_id, policy),
                )
                loaded.loaded_uris.add(uri)
                uri_to_id[uri] = context_id

            for obj in _iter_records(world, "objects", "business_objects"):
                object_id = _record_name(obj, "object_id", "id", "name")
                uri = mapping.object_uri(object_id)
                owner_space = _record_value(obj, "owner_space") or _infer_object_owner_space(obj)
                context_id = await _upsert_context(
                    db,
                    uri=uri,
                    context_type="resource",
                    scope=_record_value(obj, "scope") or "team",
                    owner_space=owner_space,
                    version=_record_version(obj),
                    tags=["entcollab", "object"],
                    content=_content("object", object_id, obj),
                )
                loaded.loaded_uris.add(uri)
                loaded.object_id_to_uri[object_id] = uri
                uri_to_id[uri] = context_id

                for dependency_ref in _dependency_refs(obj):
                    dependency_uri = _base_uri(dependency_ref)
                    dependency_id = uri_to_id.get(dependency_uri)
                    if dependency_id is not None:
                        await _insert_dependency(db, context_id, dependency_id)

        return loaded


def _iter_records(world: Any, *names: str) -> Iterable[Any]:
    for name in names:
        value = _record_value(world, name)
        if value is None:
            continue
        if isinstance(value, Mapping):
            return [_with_name(key, item) for key, item in value.items()]
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            return value
    return []


def _with_name(key: Any, item: Any) -> Any:
    if isinstance(item, Mapping):
        out = dict(item)
        out.setdefault("name", str(key))
        return out
    return {"name": str(key), "value": item}


def _record_value(record: Any, key: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(key)
    return getattr(record, key, None)


def _record_name(record: Any, *keys: str) -> str:
    if isinstance(record, str):
        return record
    for key in keys:
        value = _record_value(record, key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(f"record missing name field: {keys}")


def _record_version(record: Any) -> int:
    value = _record_value(record, "version")
    if value is None:
        return 1
    version = int(value)
    if version <= 0:
        raise ValueError("context version must be positive")
    return version


def _infer_object_owner_space(record: Any) -> str:
    role = _record_value(record, "agent") or _record_value(record, "role")
    if isinstance(role, str) and role.strip():
        return mapping.role_to_owner_space(role.strip())
    return "engineering"


def _dependency_refs(record: Any) -> list[str]:
    value = _record_value(record, "depends_on_uris") or _record_value(record, "context_versions")
    if value is None:
        return []
    return [str(item) for item in value]


def _base_uri(uri: str) -> str:
    return str(uri).split("@v", 1)[0]


def _content(kind: str, name: str, source: Any) -> str:
    return json.dumps(
        {
            "source": "EntCollabBench",
            "kind": kind,
            "name": name,
            "provenance": {
                "repository": "https://github.com/yutao1024/EntCollabBench",
                "commit": "9d085fcb86adaf20254c09e2ca35123e535a9643",
            },
            "record": _jsonable(source),
        },
        ensure_ascii=True,
        sort_keys=True,
    )


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


async def _upsert_context(
    db: ScopedRepo,
    *,
    uri: str,
    context_type: str,
    scope: str,
    owner_space: str | None,
    version: int,
    tags: list[str],
    content: str,
) -> uuid.UUID:
    row = await db.fetchrow(
        """
        INSERT INTO contexts (
            id, uri, context_type, scope, owner_space, account_id,
            status, version, tags, l0_content
        )
        VALUES (
            $1, $2, $3, $4, $5,
            current_setting('app.account_id'), 'active', $6, $7, $8
        )
        ON CONFLICT (account_id, uri) DO UPDATE SET
            context_type = EXCLUDED.context_type,
            scope = EXCLUDED.scope,
            owner_space = EXCLUDED.owner_space,
            status = EXCLUDED.status,
            version = EXCLUDED.version,
            tags = EXCLUDED.tags,
            l0_content = EXCLUDED.l0_content,
            updated_at = NOW()
        RETURNING id
        """,
        uuid.uuid4(),
        uri,
        context_type,
        scope,
        owner_space,
        version,
        tags,
        content,
    )
    return row["id"]


async def _insert_dependency(
    db: ScopedRepo,
    dependent_id: uuid.UUID,
    dependency_id: uuid.UUID,
) -> None:
    await db.execute(
        """
        INSERT INTO dependencies (dependent_id, dependency_id, dep_type)
        VALUES ($1, $2, 'derived_from')
        ON CONFLICT (dependent_id, dependency_id, dep_type) DO NOTHING
        """,
        dependent_id,
        dependency_id,
    )
