from __future__ import annotations

from dataclasses import dataclass

from contexthub.db.repository import ScopedRepo


@dataclass
class StalenessResult:
    uri: str
    status: str | None
    is_stale: bool
    version_mismatch: bool
    is_blocked: bool
    is_unknown: bool
    current_version: int | None = None
    expected_version: int | None = None


class StalenessChecker:
    """Read-only helper for contexts.status and contexts.version checks."""

    async def check_refs(
        self,
        db: ScopedRepo,
        refs: list[str],
    ) -> dict[str, StalenessResult]:
        expected: dict[str, int] = {}
        base_uris: list[str] = []

        for ref in refs or []:
            uri, version = _split_version_ref(ref)
            base_uris.append(uri)
            if version is not None:
                expected[uri] = version

        return await self.check_uris(db, base_uris, expected_versions=expected)

    async def check_uris(
        self,
        db: ScopedRepo,
        uris: list[str],
        expected_versions: dict[str, int] | None = None,
    ) -> dict[str, StalenessResult]:
        out: dict[str, StalenessResult] = {}
        expected_versions = expected_versions or {}

        for uri in uris or []:
            row = await db.fetchrow(
                "SELECT status, version FROM contexts WHERE uri = $1",
                uri,
            )
            status = row["status"] if row else None
            current_version = row["version"] if row else None
            expected_version = expected_versions.get(uri)
            version_mismatch = (
                expected_version is not None
                and current_version is not None
                and current_version != expected_version
            )

            out[uri] = StalenessResult(
                uri=uri,
                status=status,
                current_version=current_version,
                expected_version=expected_version,
                is_stale=(status == "stale"),
                version_mismatch=version_mismatch,
                is_blocked=(status in ("archived", "deleted") or status is None),
                is_unknown=(status is None),
            )

        return out

    async def any_stale_or_blocked(
        self,
        db: ScopedRepo,
        uris: list[str],
    ) -> list[StalenessResult]:
        results = await self.check_uris(db, uris)
        return [
            r
            for r in results.values()
            if r.is_stale or r.is_blocked or r.version_mismatch
        ]

    async def any_stale_or_blocked_refs(
        self,
        db: ScopedRepo,
        refs: list[str],
    ) -> list[StalenessResult]:
        results = await self.check_refs(db, refs)
        return [
            r
            for r in results.values()
            if r.is_stale or r.is_blocked or r.version_mismatch
        ]


def _split_version_ref(ref: str) -> tuple[str, int | None]:
    """Split runtime refs like ctx://team/policy/foo@v3 into base uri and version."""
    if "@v" not in ref:
        return ref, None

    uri, suffix = ref.rsplit("@v", 1)
    try:
        return uri, int(suffix)
    except ValueError:
        return ref, None
