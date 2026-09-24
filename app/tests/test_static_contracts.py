"""Static frontend contract checks."""

from pathlib import Path


APP_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = APP_DIR / "static"


def test_chat_quota_initial_markup_waits_for_server_status():
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "Checking chat availability..." in html
    assert "Demo chat limit —" not in html
    assert '<strong id="quota-remaining">3</strong>' not in html
    assert 'id="chat-input" placeholder="Checking chat availability..." disabled' in html
    assert 'id="chat-send" onclick="sendChat()" disabled' in html
    assert '<script src="/static/app.js?v=' in html


def test_chat_quota_javascript_fallback_is_disabled_until_status_loads():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "var chatQuotaRemaining = 0;" in js
    assert "var chatQuotaLimit = 0;" in js
    assert "var chatQuotaRemaining = 3;" not in js
    assert "var chatQuotaLimit = 3;" not in js


def test_demo_banner_exact_copy_present_in_js():
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    # Exact annotation copy required by the demo contract.
    assert "Anonymized sample data. Values perturbed; not actual HCA figures." in js
    # Watermark short form driven by CSS content.
    css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
    assert "content: 'Anonymized sample data'" in css
    assert ".demo-banner" in css
