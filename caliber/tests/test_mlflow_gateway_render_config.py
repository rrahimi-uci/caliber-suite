"""Contract for the MLflow AI Gateway's startup config renderer.

The gateway validates its entire endpoint list before serving. A ``$VAR``
placeholder that resolves to nothing therefore aborted startup, and with
``restart: unless-stopped`` that became a crash loop that took every *other*
endpoint down with it — a single unset ANTHROPIC_API_KEY silenced three
working OpenAI endpoints and left the CALIBER Gateway page unreachable.

These tests pin the behaviour that replaced it: serve what is configured, skip
what is not, and fail loudly only when nothing is left to serve.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to inspect gateway configs")

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_DIR = REPO_ROOT / "deploy" / "mlflow-gateway"
RENDERER = GATEWAY_DIR / "render_config.py"
SHIPPED_CONFIG = GATEWAY_DIR / "gateway.yaml"


def _load_renderer() -> Any:
    """Import the renderer by path: it ships in the image, not as a package."""
    spec = importlib.util.spec_from_file_location("render_config", RENDERER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


render_config = _load_renderer()


def _config(*endpoints: dict[str, Any]) -> dict[str, Any]:
    return {"endpoints": list(endpoints)}


def _endpoint(name: str, key_ref: str) -> dict[str, Any]:
    return {
        "name": name,
        "endpoint_type": "llm/v1/chat",
        "model": {
            "provider": "openai",
            "name": "gpt-4o-mini",
            "config": {"openai_api_key": key_ref},
        },
    }


def test_endpoint_with_resolvable_key_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    rendered, skipped = render_config.render(_config(_endpoint("chat", "$OPENAI_API_KEY")))
    assert [e["name"] for e in rendered["endpoints"]] == ["chat"]
    assert skipped == []


def test_endpoint_with_unset_key_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rendered, skipped = render_config.render(_config(_endpoint("chat", "$ANTHROPIC_API_KEY")))
    assert rendered["endpoints"] == []
    assert skipped == [("chat", ["ANTHROPIC_API_KEY"])]


def test_empty_string_counts_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose's ``${VAR:-}`` default is the exact shape that crashed the gateway."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    rendered, skipped = render_config.render(_config(_endpoint("chat", "$ANTHROPIC_API_KEY")))
    assert rendered["endpoints"] == []
    assert skipped == [("chat", ["ANTHROPIC_API_KEY"])]


def test_configured_endpoints_survive_an_unconfigured_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression this change exists to prevent."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rendered, skipped = render_config.render(
        _config(
            _endpoint("chat-openai", "$OPENAI_API_KEY"),
            _endpoint("embeddings-openai", "$OPENAI_API_KEY"),
            _endpoint("chat-anthropic", "$ANTHROPIC_API_KEY"),
        )
    )
    assert [e["name"] for e in rendered["endpoints"]] == ["chat-openai", "embeddings-openai"]
    assert [name for name, _ in skipped] == ["chat-anthropic"]


def test_braced_placeholder_is_recognised(monkeypatch: pytest.MonkeyPatch) -> None:
    """``${VAR}`` must filter like ``$VAR``, or it reads as a literal and is kept."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rendered, _ = render_config.render(_config(_endpoint("chat", "${ANTHROPIC_API_KEY}")))
    assert rendered["endpoints"] == []


def test_literal_key_without_placeholder_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rendered, skipped = render_config.render(_config(_endpoint("chat", "sk-literal")))
    assert [e["name"] for e in rendered["endpoints"]] == ["chat"]
    assert skipped == []


def test_endpoint_needing_several_vars_reports_all_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRESENT_KEY", "value")
    monkeypatch.delenv("MISSING_A", raising=False)
    monkeypatch.delenv("MISSING_B", raising=False)
    endpoint = {
        "name": "multi",
        "model": {
            "config": {
                "api_key": "$MISSING_B",
                "api_base": "$MISSING_A",
                "org": "$PRESENT_KEY",
            }
        },
    }
    _, skipped = render_config.render(_config(endpoint))
    assert skipped == [("multi", ["MISSING_A", "MISSING_B"])]


def test_unrelated_top_level_keys_are_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    config = _config(_endpoint("chat", "$OPENAI_API_KEY"))
    config["limits"] = {"renewal_period": "minute"}
    rendered, _ = render_config.render(config)
    assert rendered["limits"] == {"renewal_period": "minute"}


def test_config_without_endpoints_list_is_rejected() -> None:
    with pytest.raises(TypeError, match="no 'endpoints' list"):
        render_config.render({"limits": {}})


def test_main_exits_nonzero_when_nothing_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A gateway with zero endpoints must say why, not restart forever."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    source = tmp_path / "gateway.yaml"
    source.write_text(yaml.safe_dump(_config(_endpoint("chat", "$ANTHROPIC_API_KEY"))))
    destination = tmp_path / "rendered.yaml"

    exit_code = render_config.main(["render_config.py", str(source), str(destination)])

    assert exit_code == 1
    assert not destination.exists(), "a config with no endpoints must not be written"
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_main_writes_rendered_config_with_placeholders_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway resolves ``$VAR`` itself — the renderer must not inline secrets."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    source = tmp_path / "gateway.yaml"
    source.write_text(
        yaml.safe_dump(
            _config(
                _endpoint("chat-openai", "$OPENAI_API_KEY"),
                _endpoint("chat-anthropic", "$ANTHROPIC_API_KEY"),
            )
        )
    )
    destination = tmp_path / "rendered.yaml"

    assert render_config.main(["render_config.py", str(source), str(destination)]) == 0

    text = destination.read_text()
    assert "sk-secret-value" not in text, "rendered config must not inline the resolved key"
    assert "$OPENAI_API_KEY" in text
    rendered = yaml.safe_load(text)
    assert [e["name"] for e in rendered["endpoints"]] == ["chat-openai"]


def test_main_rejects_wrong_argument_count(capsys: pytest.CaptureFixture[str]) -> None:
    assert render_config.main(["render_config.py"]) == 2
    assert "usage:" in capsys.readouterr().err


def test_shipped_config_serves_openai_when_only_openai_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the renderer against the config the image actually bakes in."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rendered, skipped = render_config.render(yaml.safe_load(SHIPPED_CONFIG.read_text()))
    assert [e["name"] for e in rendered["endpoints"]] == [
        "chat-openai",
        "completions-openai",
        "embeddings-openai",
    ]
    openai_models = {
        endpoint["name"]: endpoint["model"]["name"]
        for endpoint in rendered["endpoints"]
        if endpoint["model"]["provider"] == "openai"
    }
    assert openai_models["chat-openai"] == "gpt-5.6-luna"
    # Luna is the chat default; the legacy text-completions endpoint deliberately
    # retains a compatible non-reasoning model.
    assert openai_models["completions-openai"] == "gpt-4o-mini"
    assert openai_models["embeddings-openai"] == "text-embedding-3-small"
    assert [name for name, _ in skipped] == ["chat-anthropic"]


def test_entrypoint_renders_before_serving() -> None:
    """The renderer is only useful if the entrypoint actually runs it."""
    entrypoint = (GATEWAY_DIR / "entrypoint.sh").read_text()
    assert "render_config.py" in entrypoint
    assert "MLFLOW_GATEWAY_SKIP_RENDER" in entrypoint, "keep the strict-validation escape hatch"


def test_dockerfile_ships_the_renderer() -> None:
    dockerfile = (GATEWAY_DIR / "Dockerfile").read_text()
    assert "COPY render_config.py /usr/local/bin/render_config.py" in dockerfile
