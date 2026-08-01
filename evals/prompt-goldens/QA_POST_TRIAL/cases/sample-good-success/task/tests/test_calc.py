from calc import add, sub


def test_add():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-2, -3) == -5


def test_sub():
    assert sub(5, 3) == 2


def test_sub_negative():
    assert sub(-5, 3) == -8
