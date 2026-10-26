import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import goto, pages
from test_app import _as, app_db  # noqa: F401  (fixture + signed-in session helper)

ROOT = Path(__file__).resolve().parents[1]


def test_staff_see_the_seasonals_page_and_farmers_only_when_quotes_are_on(app_db):
    app_db.execute("INSERT INTO users (user_id, tenant_id, email, role) VALUES (20, 'rcm', 's@rcm.com', 'elevator_staff')")
    assert "Grain Futures Seasonals" in pages(_as(app_db, 20)) and "Grain Futures Seasonals" in pages(_as(app_db, 2))
    assert "Grain Futures Seasonals" not in pages(_as(app_db, 1))                   # farmer, show_futures off
    app_db.execute("UPDATE tenants SET show_futures = 1 WHERE tenant_id = 'rcm'")
    assert "Grain Futures Seasonals" in pages(_as(app_db, 1))


def test_the_page_says_so_instead_of_crashing_when_the_data_feed_is_not_set_up(app_db, monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    at = goto(_as(app_db, 2), "Grain Futures Seasonals")
    assert not at.exception, at.exception
    assert any("MASSIVE_API_KEY" in e.value for e in at.error)
    assert any("Grain Futures Seasonals" in m.value for m in at.markdown)


def test_the_generated_copy_matches_the_source_app():
    src = ROOT.parent / "grain-seasonal-dashboard" / "app.py"
    if not src.exists():
        pytest.skip("source app not checked out next to the portal")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "sync_seasonals.py"), "check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_copy_leaves_out_ai_and_the_page_chrome():
    code = (ROOT / "seasonals" / "dashboard.py").read_text(encoding="utf-8")
    for gone in ("import anthropic", "set_page_config(", "render_hl_tab(", "header {visibility:hidden"):
        assert gone not in code, gone
    assert "def render()" in code and "from .massive_api import" in code
