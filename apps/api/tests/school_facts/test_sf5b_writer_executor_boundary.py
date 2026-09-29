from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import get_type_hints

from app.interfaces.http.school_facts import create_school_fact_router
from app.platform.school_facts.change_boundary import SchoolFactChangeBoundary


def test_formal_school_fact_router_accepts_the_executor_change_boundary() -> None:
    """正式 upload→preflight→确认链依赖高层端口，不锁死进程内 persistence。"""

    annotations = get_type_hints(create_school_fact_router)
    assert annotations["change_service"] is SchoolFactChangeBoundary


def test_api_executor_client_import_graph_excludes_writer_runtime() -> None:
    script = """
import sys
import app.composition.school_fact_writer_client

forbidden = {
    "app.composition.school_fact_writer_executor",
    "app.composition.school_fact_writer_settings",
    "app.composition.school_fact_persistence",
}
loaded = sorted(forbidden.intersection(sys.modules))
assert not loaded, loaded
assert "app.composition.school_fact_executor_client_settings" in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
