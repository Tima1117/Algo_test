import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIXTURES = ROOT / "tests" / "fixtures"

import pytest  # noqa: E402

from pif.http import Fetcher  # noqa: E402
from pif.robots import Robots  # noqa: E402


@pytest.fixture(autouse=True)
def _no_robots(request, monkeypatch):
    """В тестах сеть подменена: robots.txt не запрашиваем (кроме тестов, проверяющих сам robots)."""
    if "robots" not in request.node.name:
        monkeypatch.setattr(Fetcher, "_robots_for", lambda self, scheme, domain: Robots("", "pif-registry"))
