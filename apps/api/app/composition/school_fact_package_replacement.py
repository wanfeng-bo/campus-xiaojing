"""F7 package selection reuses the existing exact, field-projected domain readers."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

from app.platform.master_data_imports.school_facts_package import FullScopeInputRejected
from app.platform.master_data_imports.school_facts_replacement import (
    PreparedReplacementEnd,
    ReplacementFactSnapshot,
    prepare_package_preview,
)


def _at(value):
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _timeline(row, at):
    if row["version"] == 0:
        return "missing", {}
    segments = row.get("segments", ())
    active = [
        s
        for s in segments
        if _at(s["effective_at"]) <= at
        and (_at(s.get("valid_to")) is None or at < _at(s["valid_to"]))
    ]
    if len(active) > 1:
        raise FullScopeInputRejected("REPLACEMENT_CURRENT_CONFLICT")
    if active:
        return "active", dict(active[0]["content"])
    return (
        "future" if segments and all(_at(s["effective_at"]) > at for s in segments) else "ended"
    ), {}


class PackageReplacementReader:
    def __init__(
        self,
        *,
        partition,
        references,
        directory,
        family,
        education,
        residence,
        directory_adapter,
        family_adapter,
        residence_adapter,
        family_resolver=None,
        education_resolver=None,
        school_relation_resolver=None,
        quality_snapshots=None,
        quality_ends=None,
        quality_reviews=None,
        quality_adapters=None,
    ):
        self.partition, self.references = partition, references
        self.directory, self.family = directory, family
        self.education, self.residence = education, residence
        self.directory_adapter, self.family_adapter = directory_adapter, family_adapter
        self.residence_adapter = residence_adapter
        self.family_resolver, self.education_resolver = family_resolver, education_resolver
        self.school_relation_resolver = school_relation_resolver
        self.quality_snapshots = dict(quality_snapshots or {})
        self.quality_ends = dict(quality_ends or {})
        self.quality_reviews = dict(quality_reviews or {})
        self.quality_adapters = dict(quality_adapters or {})

    def _owner(self, actor, kind, key):
        coordinate = SimpleNamespace(
            fact_kind=kind, source_key=key, expected_version=1, content=None
        )
        return next(iter(self.partition.group(actor, (coordinate,))))

    def read(self, actor, kind, key, at):
        from app.domains.students.education_contracts import EDUCATION_CONTENT_FIELDS
        from app.platform.authorization.f2_reader_contracts import F2FactReadSelection
        from app.platform.authorization.secure_reader_contracts import SecureReadRequest

        self.references.require_coordinate(actor, kind, key, effective_at=at)
        owner = self._owner(actor, kind, key)
        segments = None
        if owner in self.quality_snapshots:
            value = self.quality_snapshots[owner](actor, kind, key, at)
            proof = value["proof"].revalidate
            fields, version = value["content"], value["version"]
            base = value["school_fact_version"]
            start, end = _at(value.get("valid_from")), _at(value.get("valid_to"))
            if start is None:
                raise FullScopeInputRejected("REPLACEMENT_QUALITY_PERIOD_UNAVAILABLE")
            state = "future" if start > at else "ended" if end and at >= end else "active"
            if state != "active":
                fields = {}
        elif owner == "education" and self.education is not None:
            result = self.education.repository.read_records(
                actor,
                kind,
                fields=tuple(sorted(EDUCATION_CONTENT_FIELDS[kind])),
                source_keys=(key,),
                as_of=at,
            )
            if len(result.records) != 1:
                raise FullScopeInputRejected("REPLACEMENT_TARGET_NOT_VERIFIABLE")
            row = result.records[0]
            segments = tuple(row.get("segments", ()))
            state, fields = _timeline(row, at)
            version, base = row["version"], result.school_fact_version

            def proof():
                result.proof.revalidate(actor=actor)
        elif owner == "residence" and self.residence is not None:
            result = self.residence.repository.read_for_maintenance(actor, kind, key)
            row = result.records[0]
            segments = tuple(row.get("segments", ()))
            state, fields = _timeline(row, at)
            version, base = row["version"], result.school_fact_version

            def proof():
                result.proof.revalidate(actor=actor)
        elif owner == "directory" and self.directory is not None:
            reader = self.directory.reader
            original = reader.read(
                SecureReadRequest(
                    actor=actor,
                    operation_code=f"school_facts.f2.{kind}.maintain",
                    selection=F2FactReadSelection(source_key=key),
                    page_size=2,
                    factual_at=at,
                )
            )
            if len(original.rows) != 1 or original.next_cursor is not None:
                # An absent/filtered old directory row cannot certify deletion.
                raise FullScopeInputRejected("REPLACEMENT_TARGET_NOT_VERIFIABLE")
            row = original.rows[0]
            current = reader.read(
                SecureReadRequest(
                    actor=actor,
                    operation_code=f"school_facts.f2.{kind}.read",
                    selection=F2FactReadSelection(source_key=key),
                    page_size=2,
                    factual_at=at,
                )
            )
            if len(current.rows) > 1 or current.next_cursor is not None:
                raise FullScopeInputRejected("REPLACEMENT_CURRENT_CONFLICT")
            version, base = row.expected_version, row.school_fact_version
            if current.rows:
                selected = current.rows[0]
                if (selected.expected_version, selected.school_fact_version) != (version, base):
                    raise FullScopeInputRejected("REPLACEMENT_SNAPSHOT_CHANGED")
                state, fields = "active", dict(selected.content)
            elif row.valid_from > at:
                state, fields = "future", {}
            elif row.valid_to is not None and at >= row.valid_to:
                state, fields = "ended", {}
            else:
                # Includes a filtered private/quality row: zero visible rows is
                # never promoted to proof that a current fact may be removed.
                raise FullScopeInputRejected("REPLACEMENT_CURRENT_NOT_VISIBLE")

            def proof():
                self._check_current(actor, kind, key, base, tuple(fields))
        elif owner == "family" and self.family is not None:
            from app.platform.school_facts.f3_policy import F3_FACT_FIELDS

            self.references.require_coordinate(actor, kind, key, tuple(F3_FACT_FIELDS[kind]))
            row, target, permit = self.family.repository._read(
                actor=actor,
                fact_kind=kind,
                source_key=key,
                creation=False,
                action="read",
                factual_at=at,
            )
            version, base = row.expected_version, row.school_fact_version
            if not version:
                state, fields = "missing", {}
            elif row.valid_from is None:
                state, fields = "no_current", {}
            elif row.valid_from > at:
                state, fields = "future", {}
            elif row.valid_to is not None and at >= row.valid_to:
                state, fields = "ended", {}
            else:
                state, fields = "active", dict(row.content)

            def proof():
                self.family.repository._revalidate(permit, actor, target, datetime.now(UTC))
                self._check_current(actor, kind, key, base, tuple(fields))
        else:
            raise FullScopeInputRejected("REPLACEMENT_OWNER_SNAPSHOT_UNAVAILABLE")
        proof()
        return ReplacementFactSnapshot(kind, key, at, version, base, state, fields, proof, segments)

    def preview(self, actor, change_set, *, source_reviews=()):
        generated = {}
        # Recomputed owned requests may safely contain correction timelines.
        # Validate the whole batch first, then reuse their current proof for each
        # exact resulting change. An upload cannot provide this trusted object.
        from app.platform.master_data_imports.school_facts_replacement import _change_material

        actual = {(c.fact_kind, c.source_key): c for c in change_set.changes}
        for reviewed in source_reviews:
            if reviewed.base_version != change_set.base_version:
                raise FullScopeInputRejected("SOURCE_DOMAIN_SNAPSHOT_CHANGED")
            reviewed.revalidate()
            for c in reviewed.changes:
                coordinate = (c.fact_kind, c.source_key)
                if (coordinate not in actual
                        or _change_material(actual[coordinate]) != _change_material(c)):
                    raise FullScopeInputRejected("PACKAGE_DOMAIN_REPLAY_CHANGED")
                generated[coordinate] = PreparedReplacementEnd(
                    (c,), reviewed.preview, reviewed.revalidate, reviewed.base_version)
        for c in change_set.changes:
            if c.origin.sheet_code.startswith("f7_replacement_") and c.cause_source_key is None:
                old = self.read(actor, c.fact_kind, c.source_key, c.effective_at)
                reviewed = self.end(actor, old)
                for expanded in reviewed.changes:
                    generated[(expanded.fact_kind, expanded.source_key)] = reviewed
        return prepare_package_preview(
            change_set=change_set,
            read_snapshot=lambda c: self.read(actor, c.fact_kind, c.source_key, c.effective_at),
            review_change=lambda c: (
                generated.get((c.fact_kind, c.source_key)) or self.review_change(actor, c)
            ),
        )

    def review_change(self, actor, change):
        """Compare the import with the same owned finite change builder used by forms.

        This does not reinterpret correction timelines or invent workflow intent.
        An input for which that exact business review is unavailable is not a
        complete confirmation preview, even when syntax preflight is clean.
        """
        from app.composition.school_fact_directory_product import FactChangeInput
        from app.composition.school_fact_education_product import EducationBatchInput
        from app.composition.school_fact_residence_product import ResidenceBatchInput

        kind, key = change.fact_kind, change.source_key
        owner = next(iter(self.partition.group(actor, (change,))))
        if owner in self.quality_reviews:
            return self.quality_reviews[owner](actor, change)
        if change.replacement_segments is not None or change.operation in {"correct", "cancel"}:
            raise FullScopeInputRejected("PACKAGE_TIMELINE_REVIEW_ADAPTER_REQUIRED")
        creation = change.expected_version == 0
        content = (
            change.content.model_dump(mode="json", exclude_unset=True) if change.content else {}
        )
        args = dict(
            fact_kind=kind,
            operation=change.operation,
            effective_at=change.effective_at,
            reason=change.correction_reason or "版本化输入的正式维护预览",
        )
        if owner == "family_quality" and owner in self.quality_adapters:
            adapter = self.quality_adapters[owner]
            fields = dict(content)
            if content:
                fields["target_ref"] = self.family.repository.reference_for(
                    actor=actor, fact_kind=fields["target_fact_kind"],
                    source_key=fields.pop("target_source_key"))
                fields["person_ref"] = self.directory.query.reference_for(
                    actor=actor, fact_kind="person", source_key=fields.pop("person_source_key"))
            reference = self.directory.query.reference_for(
                actor=actor, fact_kind=kind, source_key=key, creation=creation)
            resolver, model = adapter.resolve_change, FactChangeInput
        elif owner == "education_quality" and owner in self.quality_adapters:
            adapter = self.quality_adapters[owner]
            fields = adapter.references.public_fields(actor, content) if content else {}
            reference = adapter.references.reference_for(actor, kind, key, creation=creation)
            resolver, model = adapter.resolve_change, FactChangeInput
        elif owner == "residence_quality" and owner in self.quality_adapters:
            adapter = self.quality_adapters[owner]
            fields = adapter.public_fields(actor, content) if content else {}
            reference = adapter.reference_for(actor, key, creation=creation)
            resolver, model = adapter.resolve_change, FactChangeInput
        elif owner == "directory":
            from app.composition.school_fact_f2_query_repository import _reference_kind

            fields = {}
            for field, value in content.items():
                public = (
                    field.removesuffix("_source_key") + "_ref"
                    if field.endswith("_source_key")
                    else field
                )
                if value is not None and field.endswith("_source_key"):
                    value = self.directory.query.reference_for(
                        actor=actor,
                        fact_kind=_reference_kind(kind, field, content),
                        source_key=value,
                    )
                fields[public] = value
            reference = self.directory.query.reference_for(
                actor=actor, fact_kind=kind, source_key=key, creation=creation
            )
            resolver, model = self.directory_adapter.resolve_change, FactChangeInput
        elif owner == "family":
            fields = dict(
                self.family.repository.public_field_references(
                    actor=actor, fact_kind=kind, fields=content
                )
            )
            reference = self.family.repository.reference_for(
                actor=actor, fact_kind=kind, source_key=key, creation=creation
            )
            resolver = (
                self.school_relation_resolver
                if kind == "student_school_relationship"
                else self.family_resolver
            )
            model = FactChangeInput
        elif owner == "education":
            fields = self.education.maintenance.adapter.public_fields(actor, content)
            reference = self.education.repository.reference_for(actor, kind, key, creation=creation)
            resolver, model = self.education_resolver, EducationBatchInput
        elif owner == "residence":
            fields = self.residence_adapter.public_fields(actor, content)
            reference = self.residence.repository.reference_for(actor, kind, key, creation=creation)
            resolver, model = self.residence_adapter.resolve_change, ResidenceBatchInput
        else:
            raise FullScopeInputRejected("PACKAGE_OWNER_REVIEW_ADAPTER_UNAVAILABLE")
        if resolver is None:
            raise FullScopeInputRejected("PACKAGE_OWNER_REVIEW_ADAPTER_UNAVAILABLE")
        args["creation_ref" if creation else "target_ref"] = reference
        args["fields"] = fields
        resolved = resolver(
            actor=actor, payload=model(**args), request_id=uuid4(), effective_at=change.effective_at
        )
        if resolved.change_set.base_version < 1:
            raise FullScopeInputRejected("PACKAGE_PREVIEW_SNAPSHOT_CHANGED")

        def revalidate():
            if resolved.summary_revalidate is not None:
                resolved.summary_revalidate(actor)
            if change.expected_version:
                self.references.require_coordinate(
                    actor, kind, key, tuple(content), effective_at=change.effective_at
                )

        revalidate()
        return PreparedReplacementEnd(
            resolved.change_set.changes,
            resolved.summary.model_dump(mode="json"),
            revalidate,
            resolved.change_set.base_version,
        )

    def _check_current(self, actor, kind, key, base, fields):
        self.references.require_coordinate(actor, kind, key, fields)
        if (
            self.education is None
            or self.education.repository.projection.current_version(actor.school_id) != base
        ):
            raise FullScopeInputRejected("REPLACEMENT_SNAPSHOT_CHANGED")

    def end(self, actor, old):
        from app.composition.school_fact_directory_product import FactChangeInput
        from app.composition.school_fact_education_product import EducationBatchInput

        kind, key = old.fact_kind, old.source_key
        owner = self._owner(actor, kind, key)
        args = dict(
            fact_kind=kind,
            operation="end",
            fields={},
            effective_at=old.effective_at,
            reason="明确选定范围的完整替换：结束新输入中未保留的当前项",
        )
        if owner in self.quality_ends:
            resolved = self.quality_ends[owner](actor, old, args)
            return self._prepared_end(actor, old, resolved)
        if owner in self.quality_adapters:
            adapter = self.quality_adapters[owner]
            args["target_ref"] = (
                adapter.references.reference_for(actor, kind, key)
                if owner == "education_quality"
                else self.directory.query.reference_for(actor=actor, fact_kind=kind, source_key=key)
                if owner == "family_quality"
                else adapter.reference_for(actor, key)
            )
            resolved = adapter.resolve_change(
                actor=actor,
                payload=FactChangeInput(**args),
                request_id=uuid4(),
                effective_at=old.effective_at,
            )
            return self._prepared_end(actor, old, resolved)
        if owner == "directory":
            args["target_ref"] = self.directory.query.reference_for(
                actor=actor, fact_kind=kind, source_key=key
            )
            adapter, payload = self.directory_adapter, FactChangeInput(**args)
        elif owner == "education":
            args["target_ref"] = self.education.repository.reference_for(actor, kind, key)
            adapter = (
                SimpleNamespace(resolve_change=self.education_resolver)
                if (self.education_resolver is not None)
                else self.education.maintenance
            )
            payload = EducationBatchInput(**args)
        elif owner == "family":
            args["target_ref"] = self.family.repository.reference_for(
                actor=actor, fact_kind=kind, source_key=key
            )
            resolver = (
                self.school_relation_resolver
                if kind == "student_school_relationship"
                else self.family_resolver
            )
            if resolver is None:
                raise FullScopeInputRejected("REPLACEMENT_FAMILY_IMPACT_PREVIEW_REQUIRED")
            adapter, payload = SimpleNamespace(resolve_change=resolver), FactChangeInput(**args)
        elif owner == "residence":
            from app.composition.school_fact_residence_product import ResidenceBatchInput

            args["target_ref"] = self.residence.repository.reference_for(actor, kind, key)
            adapter, payload = self.residence_adapter, ResidenceBatchInput(**args)
        else:
            raise FullScopeInputRejected("REPLACEMENT_OWNER_IMPACT_PREVIEW_UNAVAILABLE")
        resolved = adapter.resolve_change(
            actor=actor, payload=payload, request_id=uuid4(), effective_at=old.effective_at
        )
        return self._prepared_end(actor, old, resolved)

    @staticmethod
    def _prepared_end(actor, old, resolved):
        if resolved.change_set.base_version != old.school_fact_version:
            raise FullScopeInputRejected("REPLACEMENT_SNAPSHOT_CHANGED")
        if resolved.summary.impacts and resolved.summary_revalidate is None:
            raise FullScopeInputRejected("REPLACEMENT_IMPACT_READ_PROOF_UNAVAILABLE")

        def revalidate():
            if resolved.summary_revalidate is not None:
                resolved.summary_revalidate(actor)
            old.revalidate()

        revalidate()
        return PreparedReplacementEnd(
            resolved.change_set.changes,
            resolved.summary.model_dump(mode="json"),
            revalidate,
            resolved.change_set.base_version,
        )
