import pytest


@pytest.mark.integration
def test_postgres_guard_probe() -> None:
    """提供不会访问数据库的集成测试探针，用于验证前置安全门。"""


def test_postgres_guard_unmarked_probe() -> None:
    """模拟忘记添加 integration 标记的测试，验证全局安全门仍然生效。"""
