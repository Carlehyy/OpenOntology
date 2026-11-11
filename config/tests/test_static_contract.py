from __future__ import annotations

import re
from pathlib import Path

from app.models import default_profile


STATIC_ROOT = Path(__file__).resolve().parents[1] / "static"


def _leaf_paths(value: object, prefix: str = "") -> set[str]:
    if not isinstance(value, dict):
        return {prefix}
    result: set[str] = set()
    for key, nested in value.items():
        path = f"{prefix}.{key}" if prefix else key
        result.update(_leaf_paths(nested, path))
    return result


def test_html_form_matches_the_server_profile_exactly() -> None:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    form_paths = set(re.findall(r'\bdata-path="([^"]+)"', html))
    model_paths = _leaf_paths(default_profile().model_dump())

    assert form_paths == model_paths


def test_static_application_respects_local_security_contract() -> None:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")
    styles = (STATIC_ROOT / "styles.css").read_text(encoding="utf-8")
    combined = "\n".join((html, javascript, styles))

    assert "localStorage" not in javascript
    assert "sessionStorage" not in javascript
    assert "eval(" not in javascript
    assert "new Function" not in javascript
    assert "\u2013" not in combined
    assert "\u2014" not in combined
    assert not re.search(r"<script(?![^>]*\bsrc=)", html, re.IGNORECASE)
    assert not re.search(r"<style\b", html, re.IGNORECASE)
    assert 'src="/static/app.js"' in html
    assert 'href="/static/styles.css"' in html


def test_every_form_control_has_an_accessible_label() -> None:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    controls = re.findall(
        r"<(?:input|select|textarea)\b[^>]*\bid=\"([^\"]+)\"[^>]*>",
        html,
        re.IGNORECASE,
    )
    labelled = set(re.findall(r'<label\b[^>]*\bfor="([^"]+)"', html))

    assert controls
    assert set(controls).issubset(labelled)


def test_nats_section_is_wired_like_other_required_services() -> None:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'data-section="nats"' in html
    assert 'data-nav-section="nats"' in html
    assert 'data-test-service="nats"' in html
    assert 'data-test-message="nats"' in html
    assert 'data-secret="nats.token"' in html
    assert '"nats"' in javascript
    assert 'nats: "NATS"' in javascript


def test_kaneo_section_is_wired_like_other_required_services() -> None:
    html = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    javascript = (STATIC_ROOT / "app.js").read_text(encoding="utf-8")

    assert 'data-section="kaneo"' in html
    assert 'data-nav-section="kaneo"' in html
    assert 'data-test-service="kaneo"' in html
    assert 'data-test-message="kaneo"' in html
    assert 'data-path="kaneo.base_url"' in html
    assert 'data-path="kaneo.web_url"' in html
    assert 'data-path="kaneo.timeout_seconds"' in html
    assert '"kaneo"' in javascript
    assert 'kaneo: "Kaneo"' in javascript
    # Kaneo 不保存任何凭据，表单不得出现密钥输入框
    assert "data-secret=\"kaneo." not in html
