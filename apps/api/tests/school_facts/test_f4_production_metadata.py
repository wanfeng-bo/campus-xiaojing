"""Production ORM registration must not depend on earlier requests or pytest imports."""

import os
import subprocess
import sys
from pathlib import Path


def test_fresh_production_writer_resolves_every_education_foreign_key():
    api = Path(__file__).resolve().parents[2]
    code = f"""
import sys
sys.path.insert(0,{str(api)!r})
import app.composition.school_fact_product_writer
from app.domains.students.education_models import EducationFact,EducationVersion,EducationReference
for model in (EducationFact,EducationVersion,EducationReference):
    for foreign_key in model.__table__.foreign_keys:
        assert foreign_key.column.table.metadata is model.metadata
    tuple(model.__mapper__._sorted_tables)
print('production_education_foreign_keys_resolved')
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=api,
        capture_output=True,
        text=True,
        env={**os.environ, "APP_ENV": "test"},
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "production_education_foreign_keys_resolved"
