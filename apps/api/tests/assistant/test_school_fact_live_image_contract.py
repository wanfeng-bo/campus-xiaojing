"""The formal image must start the approved entry with its real model dependencies."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
DOCKERFILE = ROOT / "infra/docker/api.school-fact-live.Dockerfile"


def test_formal_image_installs_model_extra_and_preserves_actual_proxy_peer():
    source = DOCKERFILE.read_text(encoding="utf-8")
    pyproject = tomllib.loads((ROOT / "apps/api/pyproject.toml").read_text(encoding="utf-8"))
    assert "f2-assistant" in pyproject["project"]["optional-dependencies"]
    assert re.search(r'pip install\s+--no-cache-dir\s+"?\.\[f2-assistant\]"?', source)
    command = json.loads(re.search(r"(?m)^CMD (\[.+\])$", source).group(1))
    assert command[:3] == [
        "uvicorn",
        "app.composition.school_fact_live_product:create_app",
        "--factory",
    ]
    assert "--no-proxy-headers" in command
    assert "--proxy-headers" not in command


def test_formal_image_context_contains_writer_factory_without_starting_it():
    source = DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY apps/api/app ./app" in source
    assert "COPY apps/api/pyproject.toml ./pyproject.toml" in source
    writer = ROOT / "apps/api/app/composition/school_fact_product_writer.py"
    assert "def create_school_fact_product_writer_app()" in writer.read_text(encoding="utf-8")
