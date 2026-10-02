import json
import shutil

import pytest

from datagen.__main__ import main


@pytest.fixture(scope="session")
def generated(tmp_path_factory):
    out = tmp_path_factory.mktemp("generated")
    assert main(["--out", str(out)]) == 0
    return out


@pytest.fixture
def make_drive(generated, tmp_path):
    """A fresh, writable copy of a generated drive, plus its answer key and incoming files."""
    from harness.drive.store import Drive

    def _make(name: str):
        shutil.copytree(generated / name, tmp_path / name)
        incoming = generated / f"{name}.incoming"
        expected = json.loads((generated / f"{name}.expected.json").read_text())
        return Drive(tmp_path / name), expected, incoming
    return _make
