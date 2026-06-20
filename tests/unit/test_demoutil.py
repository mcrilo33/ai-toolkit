from telemetry._demoutil import is_even


def test_is_even_true():
    assert is_even(4) is True


def test_is_even_false():
    assert is_even(3) is False
