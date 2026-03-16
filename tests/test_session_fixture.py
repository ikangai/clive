"""Tests for eval session fixture management."""
import os
import pytest
from evals.harness.session_fixture import EvalFixture


@pytest.fixture
def fixture_dir(tmp_path):
    """Create a minimal fixture directory."""
    (tmp_path / "file1.txt").write_text("hello world\n")
    (tmp_path / "file2.txt").write_text("second file\n")
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "nested.txt").write_text("nested content\n")
    return tmp_path


def test_fixture_creates_workdir(fixture_dir):
    with EvalFixture(fixture_dir=str(fixture_dir)) as ef:
        assert os.path.isdir(ef.workdir)
        assert os.path.exists(os.path.join(ef.workdir, "file1.txt"))
        assert os.path.exists(os.path.join(ef.workdir, "subdir", "nested.txt"))


def test_fixture_creates_tmux_session(fixture_dir):
    with EvalFixture(fixture_dir=str(fixture_dir)) as ef:
        assert ef.session_name.startswith("clive_eval_")
        import libtmux
        server = libtmux.Server()
        session = server.sessions.filter(session_name=ef.session_name)
        assert len(session) == 1


def test_fixture_cleanup(fixture_dir):
    session_name = None
    workdir = None
    with EvalFixture(fixture_dir=str(fixture_dir)) as ef:
        session_name = ef.session_name
        workdir = ef.workdir
    import libtmux
    server = libtmux.Server()
    sessions = server.sessions.filter(session_name=session_name)
    assert len(sessions) == 0
    assert not os.path.exists(workdir)
