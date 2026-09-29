"""F7 OFF: existing F4 owner Product can publish two exact quality claims.

All records and grants belong to the isolated F7 test database. This test does
not assert the final original-instance runtime configuration has been enabled.
"""

import json
import os
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.composition.school_fact_directory_settings import DirectoryConfiguration
from app.composition.school_fact_education_authority import create_education_authority
from app.composition.school_fact_education_execution import EducationDependencyFinalGuard
from app.composition.school_fact_education_product import EducationBatchInput
from app.composition.school_fact_education_quality import (
    EducationQualityBridge,
    SqlEducationDependencyProjection,
)
from app.composition.school_fact_education_reader import SqlEducationProjection
from app.composition.school_fact_education_settings import EducationConfiguration
from app.composition.school_fact_persistence import SqlAlchemySchoolFactPersistence
from app.composition.school_fact_read_connection import F2ConnectionReadExecutor
from app.composition.school_facts import (
    HmacSchoolFactConfirmationCredentialIssuer,
)
from app.platform.school_facts.application import SchoolFactChangeService
from tests.school_facts import test_f3_family_postgres, test_f3_writer_postgres
from tests.school_facts.test_f3_family_postgres import existing_fixture_writer, safe_test_engine
from tests.school_facts.test_f3_writer_postgres import WriterScenario
from tests.school_facts.test_f4_writer_postgres import apply as formal_apply_v3
from tests.school_facts.test_f7_education_quality_formal_postgres import (
    _grant_education,
    _published_pair,
)
from tests.school_facts.test_f7_family_quality_postgres import (
    _connection,
    _directory,
    _grant_test_quality,
)
from tests.school_facts.test_f7_off_actual_http_postgres import _test_configuration

pytestmark = pytest.mark.skipif(
    not os.environ.get("F7_TEST_DATABASE_CONFIG") or not os.environ.get("F7_TEST_REDIS_HOST"),
    reason="owned F7 PG and Redis config required",
)


def _assembled_product(
    stack, patch, tmp_path, *, education, directory, persistence, boundary,
):
    """Use both normal factories; only substitute the in-process Writer transport.

    Reader, actual final guards, independent empty F6 dependencies, canonical
    resolver, confirm/revalidation and persistence all remain real. No original
    instance configuration, identity, facts or model service is used.
    """
    from app.composition import school_fact_product as product_factory
    from app.composition import school_fact_product_writer as writer_factory

    # A configuration reload must rebuild the normal owners, not mutate an
    # already issued authority or bypass the Writer's current read checks.
    patch = stack.enter_context(pytest.MonkeyPatch.context())
    product_data, writer_data = _test_configuration(tmp_path)
    product_data["directory"] = directory.model_dump(mode="json")
    product_data["education"] = education.model_copy(
        update={"dependency_directory": directory}
    ).model_dump(mode="json")
    writer_data.update({key: product_data[key] for key in ("reader", "directory", "education")})
    product_path, writer_path = tmp_path / "product.json", tmp_path / "writer.json"
    product_path.write_text(json.dumps(product_data), encoding="utf-8")
    writer_path.write_text(json.dumps(writer_data), encoding="utf-8")
    patch.setenv("SCHOOL_FACT_PRODUCT_CONFIG", str(product_path))
    patch.setenv("SCHOOL_FACT_F1_WRITER_CONFIG_FILE", str(writer_path))
    final_guards, kernels, products, education_components = [], [], [], []
    real_kernel = writer_factory.SqlAlchemySchoolFactWriterExecutorKernel
    real_services = product_factory.create_school_fact_product_services
    real_router = product_factory.create_education_product_router
    real_education = product_factory.create_education_components

    def kernel(**kwargs):
        final_guards.append(kwargs["final_authorization_guard"])
        result = real_kernel(**kwargs)
        kernels.append(result)
        return result

    def services(**kwargs):
        return replace(real_services(**kwargs), writer_boundary=boundary)

    def router(**kwargs):
        products.append(kwargs["product"])
        return real_router(**kwargs)

    def education_factory(**kwargs):
        result = real_education(**kwargs)
        education_components.append(result)
        return result

    patch.setattr(writer_factory, "SqlAlchemySchoolFactWriterExecutorKernel", kernel)
    patch.setattr(product_factory, "create_school_fact_product_services", services)
    patch.setattr(product_factory, "create_education_product_router", router)
    patch.setattr(product_factory, "create_education_components", education_factory)
    stack.enter_context(TestClient(writer_factory.create_school_fact_product_writer_app()))
    assert len(final_guards) == 1
    persistence._final_authorization_guard = final_guards[0]
    # The transport port accepts the current login; the lower-level SQL service
    # instead requires targets authorized by the Writer. Reuse the real kernel
    # for that lookup so this test never invents/short-circuits receipt scope.
    boundary.check_own_result_by_locator = kernels[0].check_own_result_by_locator
    stack.enter_context(TestClient(product_factory.create_school_fact_product_app()))
    assert len(products) == 1 and len(education_components) == 2
    return products[0], education_components[0].dependency_references


def _grade_group_fixture(scenario, engines, actor):
    """Publish a fresh one-member campus/year set through the existing Writer."""
    token = uuid4().hex
    campus = f"campus:f7-off-{token}"
    stage = f"stage:f7-off-{token}"
    grade = f"grade:f7-off-{token}"
    calendar = f"calendar:f7-off-{token}"
    group = f"group:f7-off-{token}"
    at = datetime.now(UTC) - timedelta(seconds=3)
    writer = (engines, *existing_fixture_writer(engines))

    def change(kind, key, content):
        return {
            "fact_kind": kind, "source_key": key, "operation": "upsert",
            "expected_version": 0, "effective_at": at, "content": content,
        }

    formal_apply_v3(writer, [change("location", campus, {
        "kind": "campus", "display_name": "隔离校区", "display_path": "隔离校区",
        "status": "active",
    })])
    formal_apply_v3(writer, [
        change("education_stage", stage, {"display_name": "隔离学段"}),
        change("education_grade", grade, {
            "display_name": "隔离年级", "stage_source_key": stage, "grade_order": 1,
        }),
    ])
    formal_apply_v3(writer, [change("education_calendar_rule", calendar, {
        "display_name": "隔离学年规则", "stage_source_key": stage,
        "start_cycle": at.year, "month": 1, "day": 1,
        "timezone_name": "Asia/Shanghai", "grade_source_keys": (grade,),
        "campus_source_key": campus,
    })])
    formal_apply_v3(writer, [change("education_grade_group", group, {
        "calendar_source_key": calendar, "academic_year": at.year,
        "grade_source_key": grade, "campus_source_key": campus,
    })])
    return group, calendar, campus, at.year


@pytest.mark.parametrize("case", ("class_teacher", "grade_group_collection"))
def test_f7_off_f4_quality_product_confirm_receipt_and_authorized_read(case, tmp_path):
    config_path = Path(os.environ["F7_TEST_DATABASE_CONFIG"])
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["owner"] == "F7 isolated quality migration and reader tests"
    assert config["container"] == "campus-f7-tests-20260924"
    assert config["database"] == "f7_fixture_test"
    engines = {
        role: safe_test_engine(config_path, role)
        for role in ("migration", "runtime", "reader", "writer")
    }
    try:
        with _connection(config, "migration") as connection:
            connection.execute("SET ROLE campus_ai_db_owner")
            school = connection.execute(
                "SELECT school_id FROM school_database_anchor"
            ).fetchone()[0]
            schema = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            assert schema == "20260924_55"
        with pytest.MonkeyPatch.context() as patch, ExitStack() as runtime:
            patch.setattr(test_f3_family_postgres, "KEY", config["context"].encode())
            patch.setattr(test_f3_writer_postgres, "KEY", config["context"].encode())
            scenario = WriterScenario(engines, school)
            scenario.complete_seed_staff_context()
            identity = scenario.identity()
            actor = identity.staff()
            quality_key = f"quality:f7-off-{case}-{uuid4().hex}"
            q_create = _grant_test_quality(
                scenario, actor, quality_key, purpose="maintain", creation=True
            )
            if case == "class_teacher":
                target_key, _ = _published_pair(
                    config, school, "class_teacher_assignment", "education_class"
                )
                grants = (
                    _grant_education(
                        scenario, actor, kind="class_teacher_assignment", key=target_key,
                        field="assignment_source_key", action=action,
                    ) for action in ("read", "maintain")
                )
                bindings = tuple(grants)
                fields = {
                    "quality_kind": "source", "source_status": "available",
                    "owner_kind": "education",
                    "education_target_kind": "class_teacher_assignment",
                    "target_fact_kind": "class_teacher_assignment",
                    "field_name": "assignment_source_key",
                    "basis_reference": "隔离正式核查", "responsibility": "隔离维护者",
                    "verified_at": datetime.now(UTC).isoformat(),
                }
                campus = None
            else:
                target_key, calendar, campus, year = _grade_group_fixture(
                    scenario, engines, actor
                )
                bindings = (
                    _grant_education(
                        scenario, actor, kind="education_grade_group", key=target_key,
                        field=("grade_source_key", "campus_source_key", "academic_year"),
                        action="read",
                    ),
                    _grant_education(
                        scenario, actor, kind="education_grade_group", key=target_key,
                        field="grade_source_key", action="maintain",
                    ),
                    _grant_education(
                        scenario, actor, kind="education_calendar_rule", key=calendar,
                        field="display_name", action="read",
                    ),
                )
                fields = {
                    "quality_kind": "coverage", "coverage_status": "complete",
                    "owner_kind": "education",
                    "education_target_kind": "education_calendar_rule",
                    "target_fact_kind": "education_grade_group",
                    "field_name": "grade_source_key", "academic_year": year,
                    "basis_reference": "隔离学年校区完整核查", "responsibility": "隔离维护者",
                    "verified_at": datetime.now(UTC).isoformat(),
                }
            grants = [q_create]
            if campus is not None:
                grants.append(_grant_test_quality(
                    scenario, actor, campus, purpose="read", creation=False,
                    fact_kind="location",
                ))
            directory = _directory(
                scenario, config, tuple(grants),
                source_catalog={"location": [campus]} if campus is not None else {},
                creation_catalog={"fact_quality": {quality_key: "隔离教育质量"}},
            )
            authority = create_education_authority(
                sessions=scenario.sessions["reader"],
                configuration=EducationConfiguration(
                    approval_reference="F7 isolated fixture only", scope_bindings=bindings,
                ),
            )
            projection = SqlEducationProjection(
                F2ConnectionReadExecutor(
                    scenario.sessions["reader"], authority.connection_authority,
                ),
                current_sessions=authority.connection_authority.current_sessions,
            )
            quality = EducationQualityBridge(
                f2_guard=directory.guard, education_authority=authority,
                projection=SqlEducationDependencyProjection(projection),
            )
            persistence = SqlAlchemySchoolFactPersistence(
                scenario.sessions["writer"], identity.envelope,
                confirmation_credential_issuer=HmacSchoolFactConfirmationCredentialIssuer(
                    config["context"].encode()
                ),
                final_authorization_guard=EducationDependencyFinalGuard(
                    quality=quality,
                    retained=lambda *_args: pytest.fail("Unexpected noneducation partition"),
                ),
            )
            boundary = SchoolFactChangeService(reader=persistence, writer=persistence)
            hidden_failures = []
            original_confirm = boundary.confirm

            def observe_confirm(**kwargs):
                try:
                    return original_confirm(**kwargs)
                except Exception as exc:
                    hidden_failures.append(type(exc).__name__)
                    raise

            boundary.confirm = observe_confirm
            product, references = _assembled_product(
                runtime, patch, tmp_path,
                education=EducationConfiguration(
                    approval_reference="F7 isolated fixture only", scope_bindings=bindings,
                ),
                directory=DirectoryConfiguration(
                    approval_reference="F7 isolated fixture only", scope_bindings=tuple(grants),
                    source_catalog={"location": (campus,)} if campus is not None else {},
                    creation_catalog={"fact_quality": {quality_key: "隔离教育质量"}},
                ),
                boundary=boundary, persistence=persistence,
            )
            creation_ref = references.reference_for(
                actor, "fact_quality", quality_key, creation=True
            )
            if case == "class_teacher":
                target_ref = references.reference_for(
                    actor, "class_teacher_assignment", target_key
                )
                fields.update({
                    "education_target_ref": target_ref, "target_ref": target_ref,
                })
            else:
                fields.update({
                    "education_target_ref": references.reference_for(
                        actor, "education_calendar_rule", calendar
                    ),
                    "campus_ref": references.reference_for(actor, "location", campus),
                })
            payload = EducationBatchInput(
                fact_kind="fact_quality", creation_ref=creation_ref,
                fields=fields, reason="登记正式教育质量",
            )
            proposal = product.prepare(
                actor=actor, session_binding=identity.session.session_nonce_hash,
                payload=payload, request_id=uuid4(),
            )
            receipt = product.confirm(
                actor=actor, session_binding=identity.session.session_nonce_hash,
                proposal_ref=proposal.proposal_ref, request_id=uuid4(),
            )
            assert receipt.status == "succeeded", hidden_failures
            with pytest.raises(PermissionError):
                product.receipt(
                    actor=actor, session_binding=identity.session.session_nonce_hash,
                    proposal_ref=proposal.proposal_ref, request_id=uuid4(),
                )
            runtime.close()
            q_read = _grant_test_quality(
                scenario, actor, quality_key, purpose="read", creation=False
            )
            product, _ = _assembled_product(
                runtime, patch, tmp_path,
                education=EducationConfiguration(
                    approval_reference="F7 isolated fixture only", scope_bindings=bindings,
                ),
                directory=DirectoryConfiguration(
                    approval_reference="F7 isolated fixture only",
                    scope_bindings=(*grants, q_read),
                    source_catalog={
                        "fact_quality": (quality_key,),
                        **({"location": (campus,)} if campus is not None else {}),
                    },
                    creation_catalog={"fact_quality": {quality_key: "隔离教育质量"}},
                ),
                boundary=boundary, persistence=persistence,
            )
            assert product.receipt(
                actor=actor, session_binding=identity.session.session_nonce_hash,
                proposal_ref=proposal.proposal_ref, request_id=uuid4(),
            ).status == "succeeded"
            read_directory = _directory(
                scenario, config, (*grants, q_read),
                source_catalog={"fact_quality": [quality_key]},
            )
            read_quality = EducationQualityBridge(
                f2_guard=read_directory.guard, education_authority=authority,
                projection=SqlEducationDependencyProjection(projection),
            )
            result = read_quality.read_for_source(
                actor, "fact_quality", quality_key, datetime.now(UTC)
            )
            result["proof"].revalidate()
            assert result["content"]["target_fact_kind"] == (
                "class_teacher_assignment" if case == "class_teacher"
                else "education_grade_group"
            )
    finally:
        for engine in engines.values():
            engine.dispose()
