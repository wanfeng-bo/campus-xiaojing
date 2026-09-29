"""Build explicit replacement endings from one authorized selected-set snapshot."""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from typing import Any

from app.platform.master_data_imports.school_facts_package import (
    FullScopeInputManifestV5,
    FullScopeInputRejected,
    FullScopeSourcePart,
)
from app.platform.school_facts.contracts import CanonicalFactChangeV1
from app.platform.school_facts.replacement_contracts import PackageReplacementTarget


@dataclass(frozen=True)
class ReplacementFactSnapshot:
    fact_kind: str
    source_key: str
    effective_at: datetime
    expected_version: int
    school_fact_version: int
    state: str
    fields: dict[str, Any] = field(repr=False)
    revalidate: Callable[[], None] = field(repr=False)
    segments: tuple[dict[str, Any], ...] | None = field(default=None, repr=False)

    def target(self) -> PackageReplacementTarget:
        return PackageReplacementTarget.model_validate_json(
            json.dumps(
                {
                    "fact_kind": self.fact_kind,
                    "source_key": self.source_key,
                    "effective_at": self.effective_at.isoformat(),
                    "expected_version": self.expected_version,
                    "state": self.state,
                    "field_names": sorted(self.fields),
                    "content_sha256": sha256(
                        json.dumps(
                            self.fields,
                            sort_keys=True,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        ).encode()
                    ).hexdigest(),
                }
            )
        )


@dataclass(frozen=True)
class SelectedReplacement:
    targets: tuple[PackageReplacementTarget, ...]
    generated_changes: tuple[CanonicalFactChangeV1, ...]
    preview: tuple[dict[str, Any], ...] = field(repr=False)
    revalidate: Callable[[], None] = field(repr=False)


@dataclass(frozen=True)
class PreparedReplacementEnd:
    changes: tuple[CanonicalFactChangeV1, ...]
    preview: dict[str, Any] = field(repr=False)
    revalidate: Callable[[], None] = field(repr=False)
    base_version: int | None = None


@dataclass(frozen=True)
class PackageMaintenancePreview:
    status: str
    issues: tuple[str, ...]
    changes: tuple[dict[str, Any], ...] = field(repr=False)
    revalidate: Callable[[], None] = field(repr=False)


def _change_material(change):
    # Input provenance may differ from the same owned domain operation. Execution
    # meaning, including all intervals and causes, must match exactly.
    return change.model_dump(mode="json", exclude={"origin", "schema_version"}, exclude_none=True)


def prepare_package_preview(*, change_set, read_snapshot, review_change):
    actual = {(c.fact_kind, c.source_key): c for c in change_set.changes}
    issues, rows, proofs, linked = [], [], [], set()
    for change in change_set.changes:
        old = read_snapshot(change) if change.expected_version else None
        if old is not None and (
            old.expected_version != change.expected_version
            or old.school_fact_version != change_set.base_version
        ):
            raise FullScopeInputRejected("PACKAGE_PREVIEW_SNAPSHOT_CHANGED")
        if old is not None:
            proofs.append(old.revalidate)
        domain = None
        try:
            reviewed = review_change(change)
        except FullScopeInputRejected as exc:
            issues.append(exc.code)
        except ValueError:
            issues.append("PACKAGE_DOMAIN_REVIEW_REJECTED")
        else:
            if not isinstance(reviewed, PreparedReplacementEnd):
                issues.append("PACKAGE_DOMAIN_PREVIEW_UNAVAILABLE")
            else:
                if (
                    reviewed.base_version is not None
                    and reviewed.base_version != change_set.base_version
                ):
                    raise FullScopeInputRejected("PACKAGE_PREVIEW_SNAPSHOT_CHANGED")
                domain = reviewed.preview
                proofs.append(reviewed.revalidate)
                for owned in reviewed.changes:
                    coordinate = (owned.fact_kind, owned.source_key)
                    supplied = actual.get(coordinate)
                    if supplied is None:
                        issues.append("PACKAGE_DOMAIN_IMPACT_MISSING")
                    elif _change_material(supplied) != _change_material(owned):
                        issues.append("PACKAGE_DOMAIN_CHANGE_MISMATCH")
                    if coordinate != (change.fact_kind, change.source_key):
                        linked.add(coordinate)
        if change.replacement_segments is not None and old is not None and old.segments is None:
            issues.append("PACKAGE_TIMELINE_PREVIEW_UNAVAILABLE")
        rows.append(
            {
                "fact_kind": change.fact_kind,
                "source_key": change.source_key,
                "operation": change.operation,
                "effective_at": change.effective_at.isoformat(),
                "expected_version": change.expected_version,
                "origin": "replacement_end"
                if change.origin.sheet_code.startswith("f7_replacement_")
                else "explicit",
                "before": old.fields if old is not None and old.state == "active" else None,
                "after": change.content.model_dump(mode="json", exclude_unset=True)
                if change.content is not None
                else None,
                "before_segments": list(old.segments or ()) if old is not None else [],
                "after_segments": [
                    s.model_dump(mode="json", exclude_none=True)
                    for s in change.replacement_segments or ()
                ],
                "domain_preview": domain,
            }
        )
    for row in rows:
        if (row["fact_kind"], row["source_key"]) in linked:
            row["origin"] = "linked"

    def revalidate():
        for proof in proofs:
            proof()

    revalidate()
    return PackageMaintenancePreview(
        "incomplete" if issues else "complete",
        tuple(dict.fromkeys(issues)),
        tuple(rows),
        revalidate,
    )


def prepare_selected_replacement(
    *,
    manifest: FullScopeInputManifestV5,
    parts: tuple[FullScopeSourcePart, ...],
    read_snapshot: Callable,
    prepare_end: Callable,
) -> SelectedReplacement:
    if manifest.mode != "replace":
        raise FullScopeInputRejected("REPLACEMENT_MODE_REQUIRED")
    selections = {
        (s.fact_kind, key): s.effective_at
        for s in manifest.replacement_scopes
        for key in s.source_keys
    }
    incoming = {(c.fact_kind, c.source_key): c for p in parts for c in p.changes}
    if len(incoming) != sum(len(p.changes) for p in parts):
        raise FullScopeInputRejected("REPLACEMENT_INPUT_DUPLICATE")
    if set(incoming) - set(selections):
        raise FullScopeInputRejected("REPLACEMENT_INPUT_OUTSIDE_SELECTION")
    snapshots, generated, preview, end_proofs = [], [], [], []
    for (kind, key), at in selections.items():
        old = read_snapshot(kind, key, at)
        if not isinstance(old, ReplacementFactSnapshot) or (
            old.fact_kind,
            old.source_key,
            old.effective_at,
            old.school_fact_version,
        ) != (kind, key, at, manifest.base_version):
            raise FullScopeInputRejected("REPLACEMENT_SNAPSHOT_CHANGED")
        target = old.target()
        explicit = incoming.get((kind, key))
        if explicit is not None and (
            explicit.expected_version != old.expected_version or explicit.effective_at != at
        ):
            raise FullScopeInputRejected("REPLACEMENT_INPUT_SNAPSHOT_MISMATCH")
        snapshots.append(old)
        action = "explicit" if explicit else "unchanged"
        domain_preview = None
        if explicit is None and target.state == "active":
            prepared = prepare_end(old)
            if isinstance(prepared, PreparedReplacementEnd):
                expanded = prepared.changes
                domain_preview = prepared.preview
                end_proofs.append(prepared.revalidate)
            else:
                expanded = tuple(prepared)
            primary = [c for c in expanded if (c.fact_kind, c.source_key) == (kind, key)]
            if len(primary) != 1 or primary[0].expected_version != old.expected_version:
                raise FullScopeInputRejected("REPLACEMENT_END_NOT_VERIFIED")
            # A domain may express end as an equivalent temporal correction,
            # but that is its existing owned builder's decision, never the file's.
            if primary[0].operation not in {"end", "correct"}:
                raise FullScopeInputRejected("REPLACEMENT_END_NOT_VERIFIED")
            generated.extend(expanded)
            action = "end"
        preview.append(
            {
                "fact_kind": kind,
                "source_key": key,
                "effective_at": at.isoformat(),
                "expected_version": old.expected_version,
                "state": target.state,
                "selection_action": action,
                "before": old.fields,
                **({"domain_preview": domain_preview} if domain_preview else {}),
            }
        )
    seen = set(incoming)
    for change in generated:
        coordinate = (change.fact_kind, change.source_key)
        if coordinate in seen:
            raise FullScopeInputRejected("REPLACEMENT_IMPACT_OVERLAP")
        seen.add(coordinate)

    def revalidate():
        for old in snapshots:
            old.revalidate()
        for proof in end_proofs:
            proof()

    revalidate()
    return SelectedReplacement(
        tuple(s.target() for s in snapshots), tuple(generated), tuple(preview), revalidate
    )
