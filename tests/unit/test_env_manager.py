"""Tests for src.admin.env_manager — .env file read/write utility."""

from __future__ import annotations

import pytest

from src.admin.env_manager import read_env, write_env, remove_keys


@pytest.fixture()
def env_path(tmp_path):
    return tmp_path / ".env"


class TestReadEnv:

    def test_empty_file(self, env_path):
        env_path.write_text("")
        assert read_env(env_path) == {}

    def test_nonexistent_file(self, env_path):
        assert read_env(env_path) == {}

    def test_simple_kv(self, env_path):
        env_path.write_text("FOO=bar\nBAZ=123\n")
        result = read_env(env_path)
        assert result == {"FOO": "bar", "BAZ": "123"}

    def test_quoted_values(self, env_path):
        env_path.write_text('KEY="hello world"\nSINGLE=\'quoted\'\n')
        result = read_env(env_path)
        assert result["KEY"] == "hello world"
        assert result["SINGLE"] == "quoted"

    def test_comments_and_blanks(self, env_path):
        env_path.write_text("# comment\n\nFOO=bar\n  # another\n")
        result = read_env(env_path)
        assert result == {"FOO": "bar"}

    def test_export_prefix(self, env_path):
        env_path.write_text("export FOO=bar\n")
        result = read_env(env_path)
        assert result == {"FOO": "bar"}


class TestWriteEnv:

    def test_create_new(self, env_path):
        write_env(env_path, {"FOO": "bar", "BAZ": "123"})
        result = read_env(env_path)
        assert result["FOO"] == "bar"
        assert result["BAZ"] == "123"

    def test_update_existing(self, env_path):
        env_path.write_text("FOO=old\nBAR=keep\n")
        write_env(env_path, {"FOO": "new"})
        result = read_env(env_path)
        assert result["FOO"] == "new"
        assert result["BAR"] == "keep"

    def test_preserves_comments(self, env_path):
        env_path.write_text("# Header comment\nFOO=bar\n# Footer\n")
        write_env(env_path, {"FOO": "updated"})
        text = env_path.read_text()
        assert "# Header comment" in text
        assert "# Footer" in text

    def test_append_new_key(self, env_path):
        env_path.write_text("EXISTING=yes\n")
        write_env(env_path, {"NEW_KEY": "value"})
        result = read_env(env_path)
        assert result["EXISTING"] == "yes"
        assert result["NEW_KEY"] == "value"


class TestRemoveKeys:

    def test_remove_key(self, env_path):
        env_path.write_text("FOO=bar\nSECRET=hidden\nBOT=yes\n")
        remove_keys(env_path, {"SECRET"})
        result = read_env(env_path)
        assert "SECRET" not in result
        assert result["FOO"] == "bar"
        assert result["BOT"] == "yes"

    def test_remove_nonexistent(self, env_path):
        env_path.write_text("FOO=bar\n")
        remove_keys(env_path, {"MISSING"})
        result = read_env(env_path)
        assert result == {"FOO": "bar"}

    def test_remove_from_nonexistent_file(self, env_path):
        # Should not raise
        remove_keys(env_path, {"FOO"})
