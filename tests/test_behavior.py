import os

import pytest
import runez
from mock import patch

from pickley import system
from pickley.context import ImplementationMap
from pickley.lock import SharedVenv, SoftLock, SoftLockException
from pickley.settings import Settings

from .conftest import verify_abort


def test_lock(temp_base):
    folder = os.path.join(temp_base, "foo")
    with SoftLock(folder, timeout=10) as lock:
        assert lock._locked()
        with pytest.raises(SoftLockException):
            with SoftLock(folder, timeout=0.01):
                pass
        assert str(lock) == folder + ".lock"
        runez.delete(str(lock))
        assert not lock._locked()

        if runez.PY2:
            with patch("pickley.lock.virtualenv_path", return_value=None):
                assert "Can't determine path to virtualenv.py" in verify_abort(SharedVenv, lock, None)


@patch("runez.run", return_value=runez.program.RunResult("pex==1.0", "", 0))
@patch("runez.file.is_younger", return_value=True)
def test_ensure_freeze(_, __, temp_base):
    # Test edge case for _installed_module()
    with SoftLock(temp_base) as lock:
        fake_pex = os.path.join(temp_base, "bin/pex")
        runez.touch(fake_pex)
        runez.make_executable(fake_pex)
        if runez.PY2:
            v = SharedVenv(lock, None)
            assert v._installed_module(system.PackageSpec("pex"))


def test_config():
    s = Settings()
    s.load_config()
    assert len(s.config_paths) == 1
    s.load_config("foo.json")
    assert len(s.config_paths) == 2


def test_missing_implementation():
    m = ImplementationMap("custom")
    m.register(ImplementationMap)
    foo = system.PackageSpec("foo")
    assert len(m.names()) == 1
    assert "No custom type configured" in verify_abort(m.resolved, foo)
    system.SETTINGS.cli.contents["custom"] = "bar"
    assert "Unknown custom type" in verify_abort(m.resolved, foo)


def test_sorting():
    some_list = sorted([system.PackageSpec("tox"), system.PackageSpec("awscli")])
    assert [str(s) for s in some_list] == ["awscli", "tox"]
