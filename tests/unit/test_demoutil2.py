import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from telemetry._demoutil2 import is_odd


def test_is_odd_true_for_odd():
    assert is_odd(3) is True


def test_is_odd_false_for_even():
    assert is_odd(4) is False
