from app.calculator import add, subtract


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(7, 4) == 3
