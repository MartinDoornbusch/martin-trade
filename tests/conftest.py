import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tradebot import db  # noqa: E402


@pytest.fixture(autouse=True)
def no_proxy_env(monkeypatch):
    """Mocked HTTP tests must not route through environment proxies."""
    for var in list(__import__("os").environ):
        if var.lower().endswith("_proxy"):
            monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def memory_db():
    db._engine = None
    db._Session = None
    db.init_db("sqlite:///:memory:")
    yield
    db._engine = None
    db._Session = None
