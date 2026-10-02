import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from szu_electricity.catalog import empty_catalog
from szu_electricity.models import Location, Option
from szu_electricity.storage import Store


@pytest.fixture
def location():
    return Location("yuehai", "yuehai-main", "54", "山茶斋", "0601")


@pytest.fixture
def catalog():
    c = empty_catalog()
    c.areas[0].buildings = [Option("54", "山茶斋")] + [
        Option(str(n), f"楼栋{n}") for n in range(100, 120)
    ]
    c.areas[4].buildings = [Option("01", "梧桐树")]
    return c


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "data.sqlite3")
    await s.open()
    yield s
    await s.close()
