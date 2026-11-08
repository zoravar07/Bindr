"""End-to-end tests for the web interface and eval endpoints."""

from __future__ import annotations

import httpx
import pytest

from api.main import create_app
from core.gemini_client import GeminiClient

NO_LLM = GeminiClient(api_key="")


@pytest.mark.asyncio
async def test_web_index_and_static_files():
    app = create_app(gemini=NO_LLM)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        # GET / serves index.html
        res = await c.get("/")
        assert res.status_code == 200
        assert "text/html" in res.headers["content-type"]
        assert "Pre-Mortem" in res.text
        assert "3Dmol" in res.text

        # Static assets
        res_css = await c.get("/styles.css")
        assert res_css.status_code == 200
        assert "mol-container" in res_css.text

        res_js = await c.get("/app.js")
        assert res_js.status_code == 200
        assert "StructureViewer3D" in res_js.text


@pytest.mark.asyncio
async def test_api_eval_endpoint():
    app = create_app(gemini=NO_LLM)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get("/api/eval")
        assert res.status_code == 200
        data = res.json()
        assert "results" in data
        assert "licences" in data
        assert len(data["licences"]) >= 7
        licence_sources = [l["source"] for l in data["licences"]]
        assert any("Open Targets" in s for s in licence_sources)
        assert any("ClinicalTrials.gov" in s for s in licence_sources)
        assert any("PDB" in s for s in licence_sources)
