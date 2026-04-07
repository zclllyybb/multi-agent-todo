from app.reporting import render_status_report


def test_render_status_report():
    report = render_status_report(
        [
            {"status": "ok", "owner": "alice", "priority": "high", "retries": 1},
            {"status": "warn", "owner": "bob", "priority": "low", "retries": 0},
        ]
    )
    assert report.splitlines() == ["OK:alice:HIGH:1", "WARN:bob:LOW:0"]
