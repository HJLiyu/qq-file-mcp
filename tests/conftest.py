from __future__ import annotations

from dataclasses import replace

import pytest

from qq_file_mcp.config import Settings


@pytest.fixture
def settings(tmp_path):
    cache = tmp_path / "qq-cache"
    cache.mkdir()
    return Settings(
        token="test-secret",
        state_dir=tmp_path / "state",
        download_dir=tmp_path / "downloads",
        allowed_roots=(cache,),
        search_timeout=2,
    )


@pytest.fixture
def configured(settings):
    return lambda **kwargs: replace(settings, **kwargs)
