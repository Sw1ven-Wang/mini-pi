from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.web.config import (
    ProviderRegistry,
    parse_allowed_root,
)
from coding_agent.web.directories import DirectoryBrowser


def test_allowed_root_browsing_returns_relative_directories_only(tmp_path):
    (tmp_path / "alpha" / "nested").mkdir(parents=True)
    (tmp_path / "beta").mkdir()
    root = parse_allowed_root(f"Projects={tmp_path}")
    browser = DirectoryBrowser([root])

    listing = browser.browse(root.id)
    assert [item.name for item in listing.directories] == ["alpha", "beta"]
    assert all(not Path(item.relative_path).is_absolute() for item in listing.directories)

    nested = browser.browse(root.id, "alpha/nested")
    assert nested.relative_path == "alpha/nested"
    assert nested.parent_path == "alpha"


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/Windows"])
def test_directory_browser_rejects_escape_and_absolute_paths(tmp_path, path):
    root = parse_allowed_root(f"Projects={tmp_path}")
    browser = DirectoryBrowser([root])
    with pytest.raises((ValueError, PermissionError, FileNotFoundError)):
        browser.resolve(root.id, path)


def test_directory_browser_rejects_symlink_escape(tmp_path):
    root_path = tmp_path / "root"
    outside = tmp_path / "outside"
    root_path.mkdir()
    outside.mkdir()
    link = root_path / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    root = parse_allowed_root(f"Projects={root_path}")
    browser = DirectoryBrowser([root])
    with pytest.raises(PermissionError):
        browser.resolve(root.id, "escape")
    assert all(item.name != "escape" for item in browser.browse(root.id).directories)


def test_provider_registry_exposes_no_secret_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "sk-super-secret-value")
    config_path = tmp_path / "providers.json"
    config_path.write_text(json.dumps({
        "providers": [{
            "id": "test-provider",
            "label": "Test Provider",
            "base_url": "https://models.example.test/v1",
            "api_key_env": "TEST_PROVIDER_KEY",
            "models": ["small", "large"],
            "default_model": "small",
        }]
    }), encoding="utf-8")

    registry = ProviderRegistry.from_file(config_path)
    payload = registry.public_profiles()[0].model_dump_json()
    assert "sk-super-secret-value" not in payload
    assert "TEST_PROVIDER_KEY" not in payload
    assert "models.example.test" not in payload
    assert registry.public_profiles()[0].configured is True


def test_provider_registry_rejects_unlisted_model(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PROVIDER_KEY", "value")
    config_path = tmp_path / "providers.json"
    config_path.write_text(json.dumps({
        "providers": [{
            "id": "test",
            "label": "Test",
            "base_url": "https://example.test/v1",
            "api_key_env": "TEST_PROVIDER_KEY",
            "models": ["allowed"],
            "default_model": "allowed",
        }]
    }), encoding="utf-8")
    registry = ProviderRegistry.from_file(config_path)
    with pytest.raises(ValueError, match="not allowed"):
        registry.create("test", "other")
