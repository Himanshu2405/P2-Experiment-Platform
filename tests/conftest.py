import uuid

import pytest


@pytest.fixture(scope="module")
def wh():
    """A throwaway BigQuery environment (four datasets) for each test module, dropped afterwards. Modules must not share one:
    experiments that overlap on the same users would contaminate each other's metrics (as they would in real life)."""
    try:
        import google.auth
        google.auth.default()
    except Exception:
        pytest.skip("no Google credentials")
    from p2.warehouse.bq import Warehouse
    w = Warehouse(env=f"test_{uuid.uuid4().hex[:8]}")
    w.ensure()
    yield w
    w.drop_all()


import sys  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))  # lets tests import the helpers in tests/fakes.py
