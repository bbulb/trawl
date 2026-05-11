from __future__ import annotations

import json

import httpx

from trawl import diagnostics


def test_exit_code_fails_only_required_failures():
    rows = [
        diagnostics.CheckResult("python", "ok", "Python runtime available", required=True),
        diagnostics.CheckResult("reranker", "warn", "reranker unavailable", required=False),
    ]
    assert diagnostics.exit_code(rows) == 0

    rows.append(
        diagnostics.CheckResult("embedding", "fail", "embedding unavailable", required=True)
    )
    assert diagnostics.exit_code(rows) == 1


def test_render_text_includes_status_and_required_marker():
    rows = [
        diagnostics.CheckResult("embedding", "fail", "ConnectError: refused", required=True),
        diagnostics.CheckResult("reranker", "warn", "not configured", required=False),
    ]

    text = diagnostics.render_text(rows)

    assert "FAIL embedding" in text
    assert "WARN reranker" in text
    assert "required" in text
    assert "optional" in text


def test_main_json_output_uses_injected_checks(capsys):
    rows = [
        diagnostics.CheckResult(
            "python",
            "ok",
            "Python runtime available",
            required=True,
            detail={"version": "3.14.4"},
        )
    ]

    code = diagnostics.main(["--json"], checks=lambda include_network: rows)

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["checks"][0]["name"] == "python"
    assert payload["checks"][0]["detail"]["version"] == "3.14.4"


def test_main_no_network_passes_flag_to_checks(capsys):
    seen = {}

    def fake_checks(include_network: bool):
        seen["include_network"] = include_network
        return [
            diagnostics.CheckResult("python", "ok", "Python runtime available", required=True)
        ]

    code = diagnostics.main(["--no-network"], checks=fake_checks)

    captured = capsys.readouterr()
    assert code == 0
    assert seen == {"include_network": False}
    assert "OK python" in captured.out


def test_embedding_endpoint_success(monkeypatch):
    seen = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"data": [{"embedding": [0.1, 0.2]}]}

    def fake_post(url, *, json, timeout):
        seen["url"] = url
        seen["json"] = json
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(diagnostics.httpx, "post", fake_post)

    row = diagnostics.check_embedding_endpoint()

    assert row.status == "ok"
    assert row.required is True
    assert row.name == "embedding"
    assert seen["url"].endswith("/embeddings")
    assert seen["json"]["input"] == ["trawl doctor smoke"]


def test_embedding_endpoint_failure_is_required_fail(monkeypatch):
    def fake_post(*_args, **_kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(diagnostics.httpx, "post", fake_post)

    row = diagnostics.check_embedding_endpoint()

    assert row.status == "fail"
    assert row.required is True
    assert "ConnectError" in row.message


def test_reranker_endpoint_failure_is_optional_warn(monkeypatch):
    def fake_post(*_args, **_kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(diagnostics.httpx, "post", fake_post)

    row = diagnostics.check_reranker_endpoint()

    assert row.status == "warn"
    assert row.required is False
    assert "ConnectError" in row.message


def test_vlm_config_reflects_env(monkeypatch):
    monkeypatch.setenv("TRAWL_VLM_URL", "http://vlm.example/v1")

    row = diagnostics.check_vlm_configured()

    assert row.status == "ok"
    assert row.required is False
    assert row.detail["url"] == "http://vlm.example/v1"
