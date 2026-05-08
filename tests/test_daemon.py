"""Tests for daemon process ownership helpers."""

import daemon


def test_path_is_within_project_accepts_project_child(tmp_path, monkeypatch):
    project = tmp_path / "OpenGiraffe"
    child = project / "daemon.py"
    child.parent.mkdir()
    child.write_text("")
    monkeypatch.setattr(daemon, "PROJECT_ROOT", str(project.resolve()))

    assert daemon._path_is_within_project(str(child))


def test_path_is_within_project_rejects_sibling_prefix(tmp_path, monkeypatch):
    project = tmp_path / "OpenGiraffe"
    sibling = tmp_path / "OpenGiraffe-old" / "daemon.py"
    sibling.parent.mkdir()
    sibling.write_text("")
    monkeypatch.setattr(daemon, "PROJECT_ROOT", str(project.resolve()))

    assert not daemon._path_is_within_project(str(sibling))
