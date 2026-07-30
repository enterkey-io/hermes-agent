"""Tests for plugins/memory/honcho/client.py — Honcho client configuration."""

import importlib.util
import json
import os
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch, MagicMock

from hermes_cli.profiles import _get_default_hermes_home

import pytest

from plugins.memory.honcho.client import (
    HonchoClientConfig,
    HonchoProfileContext,
    get_honcho_client,
    profile_host_key,
    reset_honcho_client,
    resolve_active_host,
    resolve_config_path,
    resolve_global_config_path,
)


class TestHonchoClientConfigDefaults:
    def test_default_values(self):
        config = HonchoClientConfig()
        assert config.host == "hermes"
        assert config.workspace_id == "hermes"
        assert config.api_key is None
        assert config.environment == "production"
        assert config.timeout is None
        assert config.enabled is False
        assert config.save_messages is True
        assert config.session_strategy == "per-directory"
        assert config.recall_mode == "hybrid"
        assert config.session_peer_prefix is False
        assert config.sessions == {}


class TestFromEnv:
    def test_reads_api_key_from_env(self):
        with patch.dict(os.environ, {"HONCHO_API_KEY": "test-key-123"}):
            config = HonchoClientConfig.from_env()
        assert config.api_key == "test-key-123"
        assert config.enabled is True


    def test_defaults_without_env(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove HONCHO_API_KEY if it exists
            os.environ.pop("HONCHO_API_KEY", None)
            os.environ.pop("HONCHO_ENVIRONMENT", None)
            config = HonchoClientConfig.from_env()
        assert config.api_key is None
        assert config.environment == "production"


    def test_enabled_without_api_key_when_base_url_set(self):
        """base_url alone (no API key) is sufficient to enable a local instance."""
        with patch.dict(os.environ, {"HONCHO_BASE_URL": "http://localhost:8000"}, clear=False):
            os.environ.pop("HONCHO_API_KEY", None)
            config = HonchoClientConfig.from_env()
        assert config.api_key is None
        assert config.base_url == "http://localhost:8000"
        assert config.enabled is True


class TestFromGlobalConfig:
    def test_missing_config_falls_back_to_env(self, tmp_path):
        with patch.dict(os.environ, {}, clear=True):
            config = HonchoClientConfig.from_global_config(
                config_path=tmp_path / "nonexistent.json"
            )
        # Should fall back to from_env
        assert config.enabled is False
        assert config.api_key is None


    def test_host_block_overrides_root(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "apiKey": "key",
            "workspace": "root-ws",
            "aiPeer": "root-ai",
            "hosts": {
                "hermes": {
                    "workspace": "host-ws",
                    "aiPeer": "host-ai",
                }
            }
        }))

        config = HonchoClientConfig.from_global_config(config_path=config_file)
        assert config.workspace_id == "host-ws"
        assert config.ai_peer == "host-ai"


    def test_context_tokens_explicit_sets_cap(self, tmp_path):
        """Explicit contextTokens in config sets the cap."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"apiKey": "***", "contextTokens": 1200}))
        config = HonchoClientConfig.from_global_config(config_path=config_file)
        assert config.context_tokens == 1200


    def test_recall_mode_from_config(self, tmp_path):
        """recallMode is read from config, host block wins."""
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "apiKey": "key",
            "recallMode": "tools",
            "hosts": {"hermes": {"recallMode": "context"}},
        }))
        config = HonchoClientConfig.from_global_config(config_path=config_file)
        assert config.recall_mode == "context"


    def test_corrupt_config_falls_back_to_env(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text("not valid json{{{")

        config = HonchoClientConfig.from_global_config(config_path=config_file)
        # Should fall back to from_env without crashing
        assert isinstance(config, HonchoClientConfig)


class TestResolveSessionName:
    def test_manual_override(self):
        config = HonchoClientConfig(sessions={"/home/user/proj": "custom-session"})
        assert config.resolve_session_name("/home/user/proj") == "custom-session"

    def test_derive_from_dirname(self):
        config = HonchoClientConfig()
        result = config.resolve_session_name("/home/user/my-project")
        assert result == "my-project"


    def test_per_repo_uses_git_root(self):
        config = HonchoClientConfig(session_strategy="per-repo")
        with patch.object(
            HonchoClientConfig, "_git_repo_name", return_value="hermes-agent"
        ):
            result = config.resolve_session_name("/home/user/hermes-agent/subdir")
        assert result == "hermes-agent"


class TestResolveConfigPath:
    def test_prefers_hermes_home_when_exists(self, tmp_path):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        local_cfg = hermes_home / "honcho.json"
        local_cfg.write_text('{"apiKey": "local"}')

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            result = resolve_config_path()
        assert result == local_cfg

    def test_falls_back_to_default_profile_when_no_local(self, tmp_path, monkeypatch):
        # Profile mode: HERMES_HOME points at ~/.hermes/profiles/<name>, so
        # _get_default_hermes_home() must resolve back to ~/.hermes — that's
        # the bug the HOME-anchored helper fixes (vs. blindly using Path.home()).
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        default_home = fake_home / ".hermes"
        profile_home = default_home / "profiles" / "work"
        profile_home.mkdir(parents=True)
        default_cfg = default_home / "honcho.json"
        default_cfg.write_text('{"apiKey": "default-key"}')

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        monkeypatch.delenv("HERMES_HONCHO_HOST", raising=False)

        result = resolve_config_path()

        assert _get_default_hermes_home() == default_home
        assert result == default_cfg

    def test_falls_back_to_global_without_hermes_home_env(self, tmp_path):
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()

        with patch.dict(os.environ, {}, clear=False), \
             patch.object(Path, "home", return_value=fake_home):
            os.environ.pop("HERMES_HOME", None)
            result = resolve_config_path()
        assert result == fake_home / ".honcho" / "config.json"

    def test_global_fallback_uses_home_at_call_time(self, tmp_path):
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}), \
             patch.object(Path, "home", return_value=fake_home):
            assert resolve_global_config_path() == fake_home / ".honcho" / "config.json"
            assert resolve_config_path() == fake_home / ".honcho" / "config.json"

    def test_named_profile_does_not_use_default_profile_fallback(
        self, tmp_path, monkeypatch,
    ):
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        default_home = fake_home / ".hermes"
        profile_home = default_home / "profiles" / "work"
        profile_home.mkdir(parents=True)
        default_cfg = default_home / "honcho.json"
        default_cfg.write_text(json.dumps({
            "apiKey": "default-key",
            "workspace": "default-ws",
        }))

        monkeypatch.setattr(Path, "home", lambda: fake_home)
        monkeypatch.setenv("HERMES_HOME", str(profile_home))
        monkeypatch.delenv("HERMES_HONCHO_HOST", raising=False)

        config = HonchoClientConfig.from_global_config()

        assert config.api_key is None
        assert config.workspace_id == "hermes"

    def test_from_global_config_uses_local_path(self, tmp_path):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        local_cfg = hermes_home / "honcho.json"
        local_cfg.write_text(json.dumps({
            "apiKey": "***",
            "workspace": "local-ws",
        }))

        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}), \
             patch.object(Path, "home", return_value=tmp_path):
            config = HonchoClientConfig.from_global_config()
        assert config.api_key == "***"
        assert config.workspace_id == "local-ws"


class TestResolveActiveHost:
    def test_profile_host_key_uses_honcho_safe_separator(self):
        assert profile_host_key("coder") == "hermes_coder"
        assert profile_host_key("default") == "hermes"


    def test_explicit_env_var_wins(self, tmp_path):
        context = HonchoProfileContext.for_profile(
            "coder", tmp_path / "profiles" / "coder"
        )
        with patch.dict(os.environ, {"HERMES_HONCHO_HOST": "hermes_coder"}), \
             patch(
                 "plugins.memory.honcho.client.resolve_profile_context",
                 return_value=context,
             ):
            assert resolve_active_host() == "hermes_coder"

    @pytest.mark.parametrize("override", ["hermes_alina", "hermes_unknown"])
    def test_foreign_or_unknown_env_host_fails_closed(self, override, tmp_path):
        context = HonchoProfileContext.for_profile(
            "grace", tmp_path / "profiles" / "grace"
        )
        with patch.dict(os.environ, {"HERMES_HONCHO_HOST": override}), \
             patch(
                 "plugins.memory.honcho.client.resolve_profile_context",
                 return_value=context,
             ):
            with pytest.raises(ValueError, match="not allowed for profile 'grace'"):
                resolve_active_host()

    def test_profile_name_derives_host(self, tmp_path):
        context = HonchoProfileContext.for_profile(
            "coder", tmp_path / "profiles" / "coder"
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_HONCHO_HOST", None)
            with patch(
                "plugins.memory.honcho.client.resolve_profile_context",
                return_value=context,
            ):
                assert resolve_active_host() == "hermes_coder"

    def test_default_host_does_not_override_named_profile(self, tmp_path):
        """defaultHost is not applied before active-profile resolution."""
        config_file = tmp_path / "honcho.json"
        config_file.write_text(json.dumps({
            "defaultHost": "local",
            "hosts": {"local": {"workspace": "local-ws"}},
        }))

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_HONCHO_HOST", None)
            context = HonchoProfileContext.for_profile(
                "coder", tmp_path / "profiles" / "coder"
            )
            with patch(
                "plugins.memory.honcho.client.resolve_profile_context",
                return_value=context,
            ), \
                 patch("plugins.memory.honcho.client.resolve_config_path", return_value=config_file):
                assert resolve_active_host() == "hermes_coder"

    def test_default_host_applies_to_default_profile_only(self, tmp_path):
        """default profile remains on its canonical host despite defaultHost."""
        config_file = tmp_path / "honcho.json"
        config_file.write_text(json.dumps({
            "defaultHost": "local",
            "hosts": {"local": {"workspace": "local-ws"}},
        }))

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_HONCHO_HOST", None)
            with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"), \
                 patch("plugins.memory.honcho.client.resolve_config_path", return_value=config_file):
                assert resolve_active_host() == "hermes"

    def test_default_profile_returns_hermes(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_HONCHO_HOST", None)
            with patch("hermes_cli.profiles.get_active_profile_name", return_value="default"), \
                 patch(
                     "plugins.memory.honcho.client.resolve_config_path",
                     return_value=Path("/nonexistent/honcho.json"),
                 ):
                assert resolve_active_host() == "hermes"

    def test_custom_profile_returns_hermes(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_HONCHO_HOST", None)
            with patch("hermes_cli.profiles.get_active_profile_name", return_value="custom"), \
                 patch(
                     "plugins.memory.honcho.client.resolve_config_path",
                     return_value=Path("/nonexistent/honcho.json"),
                 ):
                assert resolve_active_host() == "hermes"

    def test_profiles_import_failure_falls_back(self):
        import sys
        with patch.dict(os.environ, {}, clear=False), patch(
            "plugins.memory.honcho.client.resolve_config_path",
            return_value=Path("/nonexistent/test-honcho-config.json"),
        ):
            os.environ.pop("HERMES_HONCHO_HOST", None)
            # Temporarily remove hermes_cli.profiles to simulate import failure
            saved = sys.modules.get("hermes_cli.profiles")
            sys.modules["hermes_cli.profiles"] = None  # type: ignore
            try:
                assert resolve_active_host() == "hermes"
            finally:
                if saved is not None:
                    sys.modules["hermes_cli.profiles"] = saved
                else:
                    sys.modules.pop("hermes_cli.profiles", None)


class TestProfileScopedConfig:
    def test_from_env_uses_profile_host(self, tmp_path):
        root = tmp_path / "profiles" / "coder"
        root.mkdir(parents=True)
        (root / ".env").write_text("HONCHO_API_KEY=key\n")
        context = HonchoProfileContext.for_profile(
            "coder", root
        )
        config = HonchoClientConfig.from_env(
            host="hermes_coder",
            context=context,
        )
        assert config.host == "hermes_coder"
        assert config.workspace_id == "hermes"  # shared workspace
        assert config.ai_peer == "hermes_coder"

    def test_from_env_default_workspace_preserved_for_default_host(self):
        with patch.dict(os.environ, {"HONCHO_API_KEY": "key"}):
            config = HonchoClientConfig.from_env(host="hermes")
        assert config.host == "hermes"
        assert config.workspace_id == "hermes"

    def test_from_global_config_reads_profile_host_block(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "apiKey": "shared-key",
            "hosts": {
                "hermes": {"aiPeer": "hermes", "peerName": "alice"},
                "hermes_coder": {
                    "aiPeer": "hermes_coder",
                    "peerName": "alice-coder",
                    "workspace": "coder-ws",
                },
            },
        }))
        context = HonchoProfileContext.for_profile(
            "coder", tmp_path / "profiles" / "coder"
        )
        config = HonchoClientConfig.from_global_config(
            host="hermes_coder",
            config_path=config_file,
            context=context,
        )
        assert config.host == "hermes_coder"
        assert config.workspace_id == "coder-ws"
        assert config.ai_peer == "hermes_coder"
        assert config.peer_name == "alice-coder"

    def test_from_global_config_rejects_foreign_explicit_host(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"apiKey": "neutral-key"}))
        context = HonchoProfileContext.for_profile(
            "grace", tmp_path / "profiles" / "grace"
        )
        with pytest.raises(ValueError, match="not allowed for profile 'grace'"):
            HonchoClientConfig.from_global_config(
                host="hermes_alina",
                config_path=config_file,
                context=context,
            )

    def test_custom_root_rejects_configured_legacy_explicit_host(
        self, tmp_path, monkeypatch,
    ):
        config_file = tmp_path / "honcho.json"
        config_file.write_text(json.dumps({
            "apiKey": "neutral-key",
            "hosts": {
                "hermes.beta": {
                    "peerName": "foreign-beta-owner",
                    "aiPeer": "foreign-beta-ai",
                }
            },
        }))
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "isolated"))

        with pytest.raises(ValueError, match="not allowed for profile 'default'"):
            HonchoClientConfig.from_global_config(
                host="hermes.beta", config_path=config_file,
            )

    def test_from_global_config_auto_resolves_host(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "apiKey": "key",
            "hosts": {
                "hermes_dreamer": {"peerName": "dreamer-user"},
            },
        }))
        context = HonchoProfileContext.for_profile(
            "dreamer", tmp_path / "profiles" / "dreamer"
        )
        config = HonchoClientConfig.from_global_config(
            config_path=config_file,
            context=context,
        )
        assert config.host == "hermes_dreamer"
        assert config.peer_name == "dreamer-user"

    def test_from_global_config_does_not_authorize_legacy_dot_host_block(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({
            "apiKey": "key",
            "hosts": {
                "hermes.dreamer": {"peerName": "dreamer-user"},
            },
        }))
        context = HonchoProfileContext.for_profile(
            "dreamer", tmp_path / "profiles" / "dreamer"
        )
        config = HonchoClientConfig.from_global_config(
            host="hermes_dreamer",
            config_path=config_file,
            context=context,
        )
        assert config.host == "hermes_dreamer"
        assert config.peer_name is None
        assert config.workspace_id == "hermes_dreamer"


class TestObservationModeMigration:
    """Existing configs without explicit observationMode keep 'unified' default."""

    def test_existing_config_defaults_to_unified(self, tmp_path):
        """Config with host block but no observationMode → 'unified' (old default)."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "apiKey": "k",
            "hosts": {"hermes": {"enabled": True, "aiPeer": "hermes"}},
        }))
        cfg = HonchoClientConfig.from_global_config(config_path=cfg_file)
        assert cfg.observation_mode == "unified"

    def test_new_config_defaults_to_directional(self, tmp_path):
        """Config with no host block and no credentials → 'directional' (new default)."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({}))
        cfg = HonchoClientConfig.from_global_config(config_path=cfg_file)
        assert cfg.observation_mode == "directional"


    def test_granular_observation_overrides_preset(self, tmp_path):
        """Explicit observation object overrides both preset and migration default."""
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({
            "apiKey": "k",
            "hosts": {"hermes": {
                "enabled": True,
                "observation": {
                    "user": {"observeMe": True, "observeOthers": False},
                    "ai": {"observeMe": False, "observeOthers": True},
                },
            }},
        }))
        cfg = HonchoClientConfig.from_global_config(config_path=cfg_file)
        # observation_mode falls back to "unified" (migration), but
        # granular booleans from the observation object win
        assert cfg.user_observe_me is True
        assert cfg.user_observe_others is False
        assert cfg.ai_observe_me is False
        assert cfg.ai_observe_others is True


class TestGetHonchoClient:
    def teardown_method(self):
        reset_honcho_client()

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_target_profile_uses_only_context_root_fallbacks(
        self, tmp_path, monkeypatch,
    ):
        default_root = tmp_path / "default"
        alpha_root = default_root / "profiles" / "alpha"
        alpha_root.mkdir(parents=True)
        (default_root / ".env").write_text("HONCHO_API_KEY=default-local-key\n")
        (default_root / "config.yaml").write_text(
            "honcho:\n"
            "  base_url: http://default.invalid:8100\n"
            "  timeout: 11\n"
        )
        (default_root / "honcho.json").write_text(json.dumps({
            "hosts": {
                "hermes": {
                    "workspace": "workspace-default",
                    "peerName": "owner-default",
                    "aiPeer": "ai-default",
                }
            }
        }))
        (alpha_root / ".env").write_text("HONCHO_API_KEY=alpha-local-key\n")
        (alpha_root / "config.yaml").write_text(
            "honcho:\n"
            "  base_url: http://alpha.invalid:8101\n"
            "  timeout: 71\n"
        )
        (alpha_root / "honcho.json").write_text(json.dumps({
            "hosts": {
                "hermes_alpha": {
                    "workspace": "workspace-alpha",
                    "peerName": "owner-alpha",
                    "aiPeer": "ai-alpha",
                }
            }
        }))
        default_context = HonchoProfileContext.for_profile(
            "default",
            default_root,
        )
        context = HonchoProfileContext.for_profile(
            "alpha",
            alpha_root,
            default_root=default_root,
        )
        monkeypatch.setenv("HONCHO_API_KEY", "ambient-default-key")
        default_client = MagicMock(name="default-client")
        alpha_client = MagicMock(name="alpha-client")

        with patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"honcho": {"timeout": 9}},
        ), patch(
            "hermes_cli.config.load_config",
            return_value={
                "honcho": {
                    "base_url": "http://ambient.invalid:8199",
                    "timeout": 9,
                }
            },
        ), patch(
            "honcho.Honcho",
            side_effect=[default_client, alpha_client],
        ) as sdk:
            default_config = HonchoClientConfig.from_global_config(
                context=default_context,
            )
            assert get_honcho_client(default_config) is default_client
            alpha_config = HonchoClientConfig.from_global_config(context=context)
            assert get_honcho_client(alpha_config) is alpha_client

        assert default_config.api_key == "default-local-key"
        assert alpha_config.api_key == "alpha-local-key"
        assert sdk.call_args_list[0].kwargs == {
            "workspace_id": "workspace-default",
            "api_key": "default-local-key",
            "environment": "production",
            "base_url": "http://default.invalid:8100",
            "timeout": 11.0,
        }
        assert sdk.call_args_list[1].kwargs == {
            "workspace_id": "workspace-alpha",
            "api_key": "alpha-local-key",
            "environment": "production",
            "base_url": "http://alpha.invalid:8101",
            "timeout": 71.0,
        }

    @pytest.mark.parametrize("profile", ["default", "custom"])
    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_explicit_default_and_custom_roots_ignore_ambient_fallbacks(
        self, tmp_path, monkeypatch, profile,
    ):
        root = tmp_path / f"{profile}-root"
        root.mkdir()
        (root / ".env").write_text(f"HONCHO_API_KEY={profile}-local-key\n")
        (root / "config.yaml").write_text(
            "honcho:\n"
            f"  base_url: http://{profile}.invalid:8201\n"
            "  timeout: 82\n"
        )
        (root / "honcho.json").write_text(json.dumps({
            "hosts": {
                "hermes": {
                    "workspace": f"workspace-{profile}",
                    "peerName": f"owner-{profile}",
                    "aiPeer": f"ai-{profile}",
                }
            }
        }))
        context = HonchoProfileContext.for_profile(profile, root)
        monkeypatch.setenv("HONCHO_API_KEY", "ambient-default-key")
        expected_client = MagicMock(name=f"{profile}-client")

        with patch(
            "hermes_cli.config.load_config_readonly",
            return_value={"honcho": {"timeout": 12}},
        ), patch(
            "hermes_cli.config.load_config",
            return_value={
                "honcho": {
                    "base_url": "http://ambient.invalid:8200",
                    "timeout": 12,
                }
            },
        ), patch("honcho.Honcho", return_value=expected_client) as sdk:
            config = HonchoClientConfig.from_global_config(context=context)
            client = get_honcho_client(config)

        assert client is expected_client
        assert config.api_key == f"{profile}-local-key"
        assert sdk.call_args.kwargs["base_url"] == (
            f"http://{profile}.invalid:8201"
        )
        assert sdk.call_args.kwargs["timeout"] == 82.0
        assert sdk.call_args.kwargs["api_key"] == f"{profile}-local-key"

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_concurrent_profile_contexts_never_cross_return(
        self, tmp_path, monkeypatch,
    ):
        from plugins.memory.honcho.client import HonchoProfileContext

        configs = {}
        expected_clients = {}
        for profile in ("alpha", "beta"):
            root = tmp_path / "profiles" / profile
            root.mkdir(parents=True)
            path = root / "honcho.json"
            path.write_text(json.dumps({
                "apiKey": "neutral-key",
                "hosts": {
                    f"hermes_{profile}": {
                        "workspace": f"workspace-{profile}",
                        "peerName": f"owner-{profile}",
                        "aiPeer": f"ai-{profile}",
                    }
                },
            }))
            context = HonchoProfileContext.for_profile(profile, root)
            configs[profile] = HonchoClientConfig.from_global_config(context=context)
            expected_clients[profile] = MagicMock(name=f"{profile}-client")

        start = threading.Barrier(3)

        def build_client(**kwargs):
            profile = kwargs["workspace_id"].removeprefix("workspace-")
            return expected_clients[profile]

        def acquire(profile):
            start.wait()
            return get_honcho_client(configs[profile])

        monkeypatch.setenv("HERMES_HONCHO_HOST", "hermes_beta")
        with patch(
            "hermes_cli.profiles.get_active_profile_name",
            return_value="beta",
        ), patch("honcho.Honcho", side_effect=build_client) as sdk, ThreadPoolExecutor(
            max_workers=2
        ) as pool:
            futures = {
                profile: pool.submit(acquire, profile)
                for profile in ("alpha", "beta")
            }
            start.wait()
            results = {
                profile: future.result(timeout=5)
                for profile, future in futures.items()
            }

        assert results == expected_clients
        assert sdk.call_count == 2

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_concurrent_custom_roots_have_distinct_cache_identity(
        self, tmp_path,
    ):
        from plugins.memory.honcho.client import HonchoProfileContext

        configs = {}
        expected_clients = {}
        for name in ("one", "two"):
            root = tmp_path / name
            root.mkdir()
            (root / "honcho.json").write_text(json.dumps({
                "apiKey": "neutral-key",
                "hosts": {
                    "hermes": {
                        "workspace": f"workspace-{name}",
                        "peerName": f"owner-{name}",
                        "aiPeer": f"ai-{name}",
                    }
                },
            }))
            context = HonchoProfileContext.for_profile("custom", root)
            configs[name] = HonchoClientConfig.from_global_config(context=context)
            expected_clients[name] = MagicMock(name=f"{name}-client")

        start = threading.Barrier(3)

        def build_client(**kwargs):
            name = kwargs["workspace_id"].removeprefix("workspace-")
            return expected_clients[name]

        def acquire(name):
            start.wait()
            return get_honcho_client(configs[name])

        with patch("honcho.Honcho", side_effect=build_client) as sdk, ThreadPoolExecutor(
            max_workers=2
        ) as pool:
            futures = {
                name: pool.submit(acquire, name)
                for name in ("one", "two")
            }
            start.wait()
            results = {
                name: future.result(timeout=5)
                for name, future in futures.items()
            }

        assert results == expected_clients
        assert sdk.call_count == 2

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_cached_client_rebuilds_after_active_profile_switch(
        self, tmp_path, monkeypatch,
    ):
        current = {"profile": "alpha"}
        contexts = {}
        config_paths = {}
        for profile in ("alpha", "beta"):
            path = tmp_path / "profiles" / profile / "honcho.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "apiKey": "neutral-key",
                "hosts": {
                    f"hermes_{profile}": {
                        "workspace": "hermes",
                        "peerName": f"owner-{profile}",
                        "aiPeer": f"ai-{profile}",
                    }
                },
            }))
            config_paths[profile] = path
            contexts[profile] = HonchoProfileContext.for_profile(
                profile, path.parent
            )
        monkeypatch.delenv("HERMES_HONCHO_HOST", raising=False)
        first_client = MagicMock(name="alpha-client")
        second_client = MagicMock(name="beta-client")

        with patch(
            "honcho.Honcho",
            side_effect=[first_client, second_client],
        ) as sdk:
            first = get_honcho_client(HonchoClientConfig.from_global_config(
                context=contexts[current["profile"]],
            ))
            current["profile"] = "beta"
            second = get_honcho_client(HonchoClientConfig.from_global_config(
                context=contexts[current["profile"]],
            ))

        assert first is first_client
        assert second is second_client
        assert sdk.call_count == 2
        assert sdk.call_args_list[0].kwargs["workspace_id"] == "hermes"
        assert sdk.call_args_list[1].kwargs["workspace_id"] == "hermes"

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_explicit_context_ignores_later_process_host_override(
        self, tmp_path, monkeypatch,
    ):
        from plugins.memory.honcho.client import HonchoProfileContext

        root = tmp_path / "profiles" / "alpha"
        root.mkdir(parents=True)
        config_file = root / "honcho.json"
        config_file.write_text(json.dumps({
            "apiKey": "neutral-key",
            "hosts": {
                "hermes_alpha": {
                    "workspace": "hermes",
                    "peerName": "owner-alpha",
                    "aiPeer": "ai-alpha",
                }
            },
        }))
        monkeypatch.delenv("HERMES_HONCHO_HOST", raising=False)
        cached_client = MagicMock(name="alpha-client")
        context = HonchoProfileContext.for_profile("alpha", root)

        with patch("honcho.Honcho", return_value=cached_client) as sdk:
            config = HonchoClientConfig.from_global_config(context=context)
            assert get_honcho_client(config) is cached_client
            monkeypatch.setenv("HERMES_HONCHO_HOST", "hermes_beta")
            assert get_honcho_client(config) is cached_client

        sdk.assert_called_once()

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_passes_timeout_from_config(self):
        fake_honcho = MagicMock(name="Honcho")
        cfg = HonchoClientConfig(
            api_key="test-key",
            timeout=91.0,
            workspace_id="hermes",
            environment="production",
        )

        with patch("honcho.Honcho", return_value=fake_honcho) as mock_honcho:
            client = get_honcho_client(cfg)

        assert client is fake_honcho
        mock_honcho.assert_called_once()
        assert mock_honcho.call_args.kwargs["timeout"] == 91.0

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_hermes_config_timeout_override_used_when_config_timeout_missing(
        self, tmp_path,
    ):
        fake_honcho = MagicMock(name="Honcho")
        (tmp_path / "config.yaml").write_text("honcho:\n  timeout: 88\n")
        cfg = HonchoClientConfig(
            api_key="test-key",
            workspace_id="hermes",
            environment="production",
            profile_context=HonchoProfileContext.for_profile("custom", tmp_path),
        )

        with patch("honcho.Honcho", return_value=fake_honcho) as mock_honcho:
            client = get_honcho_client(cfg)

        assert client is fake_honcho
        mock_honcho.assert_called_once()
        assert mock_honcho.call_args.kwargs["timeout"] == 88.0

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_defaults_to_30s_when_no_timeout_configured(self):
        from plugins.memory.honcho.client import _DEFAULT_HTTP_TIMEOUT

        fake_honcho = MagicMock(name="Honcho")
        cfg = HonchoClientConfig(
            api_key="test-key",
            workspace_id="hermes",
            environment="production",
        )

        with patch("honcho.Honcho", return_value=fake_honcho) as mock_honcho, \
             patch("hermes_cli.config.load_config", return_value={}):
            client = get_honcho_client(cfg)

        assert client is fake_honcho
        mock_honcho.assert_called_once()
        assert mock_honcho.call_args.kwargs["timeout"] == _DEFAULT_HTTP_TIMEOUT

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_hermes_request_timeout_alias_used(self, tmp_path):
        fake_honcho = MagicMock(name="Honcho")
        (tmp_path / "config.yaml").write_text(
            "honcho:\n  request_timeout: 77.5\n"
        )
        cfg = HonchoClientConfig(
            api_key="test-key",
            workspace_id="hermes",
            environment="production",
            profile_context=HonchoProfileContext.for_profile("custom", tmp_path),
        )

        with patch("honcho.Honcho", return_value=fake_honcho) as mock_honcho:
            client = get_honcho_client(cfg)

        assert client is fake_honcho
        mock_honcho.assert_called_once()
        assert mock_honcho.call_args.kwargs["timeout"] == 77.5

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_timeout_change_triggers_client_rebuild(self):
        """Changing timeout config must rebuild the cached client."""
        from hermes_constants import get_hermes_home

        cfg_yaml = get_hermes_home() / "config.yaml"
        cfg_yaml.write_text("honcho:\n  timeout: 30\n")

        fake_honcho_1 = MagicMock(name="Honcho_v1")
        fake_honcho_2 = MagicMock(name="Honcho_v2")
        cfg = HonchoClientConfig(
            api_key="test-key",
            workspace_id="hermes",
            environment="production",
        )

        with patch("honcho.Honcho", return_value=fake_honcho_1) as mock_h1:
            client1 = get_honcho_client(cfg)

        assert client1 is fake_honcho_1
        assert mock_h1.call_args.kwargs["timeout"] == 30.0

        # Same config — should return cached client (no rebuild)
        with patch("honcho.Honcho", return_value=fake_honcho_2) as mock_h2:
            client2 = get_honcho_client(cfg)

        assert client2 is fake_honcho_1  # still cached
        mock_h2.assert_not_called()

        # Changed timeout — must rebuild
        cfg_yaml.write_text("honcho:\n  timeout: 300\n")
        st = cfg_yaml.stat()
        os.utime(cfg_yaml, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

        with patch("honcho.Honcho", return_value=fake_honcho_2) as mock_h3:
            client3 = get_honcho_client(cfg)

        assert client3 is fake_honcho_2  # rebuilt
        mock_h3.assert_called_once()
        assert mock_h3.call_args.kwargs["timeout"] == 300.0

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_managed_config_timeout_does_not_thrash_singleton(self, tmp_path, monkeypatch):
        """A managed-scope honcho.timeout with no user config.yaml must be seen
        by the staleness check (stable reuse), and a managed edit must trigger
        a rebuild. Regression for a memo that keyed only on the user file."""
        managed_dir = tmp_path / "managed"
        managed_dir.mkdir()
        managed_cfg = managed_dir / "config.yaml"
        managed_cfg.write_text("honcho:\n  timeout: 88\n")
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))

        fake_honcho_1 = MagicMock(name="Honcho_v1")
        fake_honcho_2 = MagicMock(name="Honcho_v2")
        cfg = HonchoClientConfig(
            api_key="test-key",
            workspace_id="hermes",
            environment="production",
        )

        with patch("honcho.Honcho", return_value=fake_honcho_1) as mock_h1:
            client1 = get_honcho_client(cfg)
            client2 = get_honcho_client(cfg)

        assert client1 is fake_honcho_1
        assert client2 is fake_honcho_1
        assert mock_h1.call_count == 1
        assert mock_h1.call_args.kwargs["timeout"] == 88.0

        # A managed-timeout edit is detected (same-size write, so bump mtime).
        managed_cfg.write_text("honcho:\n  timeout: 99\n")
        st = managed_cfg.stat()
        os.utime(managed_cfg, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

        with patch("honcho.Honcho", return_value=fake_honcho_2) as mock_h2:
            client3 = get_honcho_client(cfg)

        assert client3 is fake_honcho_2
        mock_h2.assert_called_once()
        assert mock_h2.call_args.kwargs["timeout"] == 99.0


class TestResolveSessionNameGatewayKey:
    """Regression tests for gateway_session_key priority in resolve_session_name.

    Ensures gateway platforms get stable per-chat Honcho sessions even when
    sessionStrategy=per-session would otherwise create ephemeral sessions.
    Regression: plugin refactor 924bc67e dropped gateway key plumbing.
    """

    def test_gateway_key_overrides_per_session_strategy(self):
        """gateway_session_key must win over per-session session_id."""
        config = HonchoClientConfig(session_strategy="per-session")
        result = config.resolve_session_name(
            session_id="20260412_171002_69bb38",
            gateway_session_key="agent:main:telegram:dm:8439114563",
        )
        assert result == "agent-main-telegram-dm-8439114563"


    def test_gateway_key_sanitizes_special_chars(self):
        """Colons and other non-alphanumeric chars are replaced with hyphens."""
        config = HonchoClientConfig()
        result = config.resolve_session_name(
            gateway_session_key="agent:main:telegram:dm:8439114563",
        )
        assert result == "agent-main-telegram-dm-8439114563"
        assert ":" not in result

    def test_gateway_key_is_profile_scoped_when_enabled(self):
        """Profile isolation must also apply to stable gateway sessions."""
        config = HonchoClientConfig(
            peer_name="elliott-grace",
            session_peer_prefix=True,
        )
        result = config.resolve_session_name(
            gateway_session_key="agent:main:telegram:dm:8439114563",
        )
        assert result == "elliott-grace-agent-main-telegram-dm-8439114563"


class TestResolveSessionNameLengthLimit:
    """Regression tests for Honcho's 100-char session ID limit (issue #13868).

    Long gateway session keys (Matrix room+event IDs, Telegram supergroup
    reply chains, Slack thread IDs with long workspace prefixes) can overflow
    Honcho's 100-char session_id limit after sanitization. Before this fix,
    every Honcho API call for those sessions 400'd with "session_id too long".
    """

    HONCHO_MAX = 100

    def test_short_gateway_key_unchanged(self):
        """Short keys must not get a hash suffix appended."""
        config = HonchoClientConfig()
        result = config.resolve_session_name(
            gateway_session_key="agent:main:telegram:dm:8439114563",
        )
        # Unchanged fast-path: sanitize only, no truncation, no hash suffix.
        assert result == "agent-main-telegram-dm-8439114563"
        assert len(result) <= self.HONCHO_MAX


    def test_long_gateway_key_truncated_to_limit(self):
        """An over-limit sanitized key must truncate to exactly 100 chars."""
        key = "!roomid:matrix.example.org|" + "$event_" + ("a" * 300)
        config = HonchoClientConfig()
        result = config.resolve_session_name(gateway_session_key=key)
        assert result is not None
        assert len(result) == self.HONCHO_MAX


    def test_distinct_long_keys_do_not_collide(self):
        """Two long keys sharing a prefix must produce different truncated IDs."""
        prefix = "matrix:!room:example.org|" + "a" * 200
        key_a = prefix + "-suffix-alpha"
        key_b = prefix + "-suffix-beta"
        config = HonchoClientConfig()
        result_a = config.resolve_session_name(gateway_session_key=key_a)
        result_b = config.resolve_session_name(gateway_session_key=key_b)
        assert result_a != result_b
        assert len(result_a) == self.HONCHO_MAX
        assert len(result_b) == self.HONCHO_MAX


class TestResetHonchoClient:
    def test_reset_clears_singleton(self):
        import plugins.memory.honcho.client as mod

        cache_key = ("/synthetic", "default", "hermes", False)
        mod._honcho_client_cache[cache_key] = (MagicMock(), 30.0)
        assert mod._honcho_client_cache
        reset_honcho_client()
        assert not mod._honcho_client_cache


class TestDialecticDepthParsing:
    """Tests for _parse_dialectic_depth and _parse_dialectic_depth_levels."""


    def test_depth_clamped_high(self, tmp_path):
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"apiKey": "***", "dialecticDepth": 10}))
        config = HonchoClientConfig.from_global_config(config_path=config_file)
        assert config.dialectic_depth == 3


class TestGetHonchoClientBaseUrlDoublePrefixFix:
    """Regression tests for #20688 — Honcho SDK double-prefixing of /v3 for
    self-hosted instances where base_url already contains a version path."""

    def teardown_method(self):
        reset_honcho_client()

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_local_base_url_with_v3_suffix_stripped(self):
        """base_url 'http://localhost:38000/v3' must become 'http://localhost:38000'
        before passing to the Honcho SDK to avoid double '/v3/v3' prefixing."""
        fake_honcho = MagicMock(name="Honcho")
        cfg = HonchoClientConfig(
            api_key=None,
            base_url="http://localhost:38000/v3",
            workspace_id="hermes",
            environment="production",
        )

        with patch("honcho.Honcho", return_value=fake_honcho) as mock_honcho, \
             patch("hermes_cli.config.load_config", return_value={}):
            get_honcho_client(cfg)

        mock_honcho.assert_called_once()
        passed_base_url = mock_honcho.call_args.kwargs.get("base_url")
        assert passed_base_url == "http://localhost:38000", (
            f"Expected 'http://localhost:38000', got {passed_base_url!r}"
        )


    def test_lan_default_host_empty_key_uses_local_placeholder(self, tmp_path):
        """Regression for #61661: setup-style root baseUrl + defaultHost + LAN IP
        must not pass an empty/None api_key to the SDK for a no-auth local server."""
        config_file = tmp_path / "honcho.json"
        config_file.write_text(json.dumps({
            "defaultHost": "hermes",
            "baseUrl": "http://192.168.2.112:8000",
            "hosts": {
                "hermes": {
                    "workspace": "local-ws",
                    "aiPeer": "local-ai",
                    "apiKey": "",
                },
            },
        }))

        with patch.dict(os.environ, {}, clear=True), \
             patch("hermes_cli.profiles.get_active_profile_name", return_value="default"), \
             patch("plugins.memory.honcho.client.resolve_config_path", return_value=config_file):
            cfg = HonchoClientConfig.from_global_config(config_path=config_file)

        assert cfg.host == "hermes"
        assert cfg.workspace_id == "local-ws"
        assert cfg.ai_peer == "local-ai"
        assert cfg.api_key is None
        assert cfg.base_url == "http://192.168.2.112:8000"

        fake_honcho = MagicMock(name="Honcho")
        mock_honcho = MagicMock(return_value=fake_honcho)
        fake_honcho_module = types.SimpleNamespace(Honcho=mock_honcho)
        with patch.dict(sys.modules, {"honcho": fake_honcho_module}), \
             patch("hermes_cli.config.load_config", return_value={}):
            get_honcho_client(cfg)

        mock_honcho.assert_called_once()
        assert mock_honcho.call_args.kwargs["api_key"] == "local"
        assert mock_honcho.call_args.kwargs["base_url"] == "http://192.168.2.112:8000"

    def test_lan_default_host_explicit_host_key_preserved(self, tmp_path):
        """A host-block local JWT still wins for LAN/VPN local URLs."""
        config_file = tmp_path / "honcho.json"
        config_file.write_text(json.dumps({
            "defaultHost": "hermes",
            "baseUrl": "http://192.168.2.112:8000",
            "hosts": {
                "hermes": {
                    "workspace": "local-ws",
                    "aiPeer": "local-ai",
                    "apiKey": "local-jwt",
                },
            },
        }))

        with patch.dict(os.environ, {}, clear=True), \
             patch("hermes_cli.profiles.get_active_profile_name", return_value="default"), \
             patch("plugins.memory.honcho.client.resolve_config_path", return_value=config_file):
            cfg = HonchoClientConfig.from_global_config(config_path=config_file)

        fake_honcho = MagicMock(name="Honcho")
        mock_honcho = MagicMock(return_value=fake_honcho)
        fake_honcho_module = types.SimpleNamespace(Honcho=mock_honcho)
        with patch.dict(sys.modules, {"honcho": fake_honcho_module}), \
             patch("hermes_cli.config.load_config", return_value={}):
            get_honcho_client(cfg)

        mock_honcho.assert_called_once()
        assert mock_honcho.call_args.kwargs["api_key"] == "local-jwt"

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_cloud_base_url_without_version_unchanged(self):
        """A cloud base_url with no version segment must pass through untouched."""
        fake_honcho = MagicMock(name="Honcho")
        cfg = HonchoClientConfig(
            api_key="cloud-key",
            base_url="https://api.honcho.dev",
            workspace_id="hermes",
            environment="production",
        )

        with patch("honcho.Honcho", return_value=fake_honcho) as mock_honcho, \
             patch("hermes_cli.config.load_config", return_value={}):
            get_honcho_client(cfg)

        mock_honcho.assert_called_once()
        passed_base_url = mock_honcho.call_args.kwargs.get("base_url")
        assert passed_base_url == "https://api.honcho.dev", (
            f"Expected 'https://api.honcho.dev', got {passed_base_url!r}"
        )

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    def test_cloud_base_url_with_version_stripped(self):
        """A version segment double-prefixes regardless of host, so a cloud
        base_url that ends in '/v3' must also be stripped (the SDK re-adds it)."""
        fake_honcho = MagicMock(name="Honcho")
        cfg = HonchoClientConfig(
            api_key="cloud-key",
            base_url="https://api.honcho.dev/v3",
            workspace_id="hermes",
            environment="production",
        )

        with patch("honcho.Honcho", return_value=fake_honcho) as mock_honcho, \
             patch("hermes_cli.config.load_config", return_value={}):
            get_honcho_client(cfg)

        mock_honcho.assert_called_once()
        passed_base_url = mock_honcho.call_args.kwargs.get("base_url")
        assert passed_base_url == "https://api.honcho.dev", (
            f"Expected 'https://api.honcho.dev', got {passed_base_url!r}"
        )

    @pytest.mark.skipif(
        not importlib.util.find_spec("honcho"),
        reason="honcho SDK not installed"
    )
    @pytest.mark.parametrize(
        "raw_url, expected",
        [
            # LAN IP self-host
            ("http://10.0.0.5:8000/v3", "http://10.0.0.5:8000"),
            ("http://192.168.1.20:38000/v3/", "http://192.168.1.20:38000"),
            # Tailscale / custom-domain self-host
            ("https://honcho.my.ts.net/v3", "https://honcho.my.ts.net"),
            ("https://honcho.lab.internal/v3", "https://honcho.lab.internal"),
            ("https://honcho.fly.dev/v3", "https://honcho.fly.dev"),
            # higher version segments are also stripped
            ("https://honcho.lab.internal/v12", "https://honcho.lab.internal"),
            # self-host without a version segment is left unchanged
            ("https://honcho.my.ts.net", "https://honcho.my.ts.net"),
            ("http://10.0.0.5:8000", "http://10.0.0.5:8000"),
        ],
    )
    def test_self_hosted_base_url_version_stripped(self, raw_url, expected):
        """Non-loopback self-hosted instances (LAN IPs, Tailscale, custom
        domains) must get the same version-segment stripping as localhost.
        Regression for #20688 recurring on any non-loopback self-host."""
        fake_honcho = MagicMock(name="Honcho")
        cfg = HonchoClientConfig(
            api_key="self-host-key",
            base_url=raw_url,
            workspace_id="hermes",
            environment="production",
        )

        with patch("honcho.Honcho", return_value=fake_honcho) as mock_honcho, \
             patch("hermes_cli.config.load_config", return_value={}):
            get_honcho_client(cfg)

        mock_honcho.assert_called_once()
        passed_base_url = mock_honcho.call_args.kwargs.get("base_url")
        assert passed_base_url == expected, (
            f"Expected {expected!r}, got {passed_base_url!r}"
        )
