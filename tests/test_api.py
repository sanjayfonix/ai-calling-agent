"""
Tests for the FastAPI application routes.
"""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    """Create an async test client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


class TestHealthEndpoint:

    @pytest.mark.asyncio
    async def test_health_returns_200(self, client):
        response = await client.get("/api/health")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_health_response_structure(self, client):
        response = await client.get("/api/health")
        data = response.json()
        assert "status" in data
        assert "version" in data
        assert "active_calls" in data
        assert "timestamp" in data
        assert data["status"] == "healthy"


class TestOutboundCallValidation:

    @pytest.mark.asyncio
    async def test_invalid_phone_format(self, client):
        response = await client.post(
            "/api/calls/outbound",
            json={"to_number": "1234567890"},  # Missing +
        )
        assert response.status_code == 400
        assert "E.164" in response.json()["detail"]
