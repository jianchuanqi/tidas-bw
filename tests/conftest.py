from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session")
def isolated_brightway_dir(tmp_path_factory):
    brightway_dir = tmp_path_factory.mktemp("brightway-data")
    os.environ["BRIGHTWAY2_DIR"] = str(brightway_dir)
    return brightway_dir
