from app.formatting import render_math


def test_render_math():
    assert render_math("sum", 5) == "sum=5"
