"""Internal API key protection for scraper/ingest/admin report write routes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import official_filings, scraper
from app.core.csrf import CSRFMiddleware
from app.core.database import get_db
from app.core.security import verify_internal_key


VALID_KEY = "test-internal-key-for-ingest-routes-only"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr("app.core.security.settings.INTERNAL_API_KEY", VALID_KEY)
    monkeypatch.setattr("app.core.config.settings.INTERNAL_API_KEY", VALID_KEY)

    application = FastAPI()
    application.add_middleware(CSRFMiddleware)
    application.include_router(scraper.router)
    application.include_router(official_filings.router, prefix="/api")

    db = MagicMock()

    def override_db():
        yield db

    application.dependency_overrides[get_db] = override_db
    application.state.test_db = db
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        c.app = app
        yield c


class TestInternalKeyRequired:
    def test_ingest_no_key_rejected(self, client):
        res = client.post(
            "/api/scraper/ingest",
            json={
                "company_symbol": "1111",
                "company_name_en": "T",
                "reports": [],
            },
        )
        assert res.status_code == 401
        assert "key" in res.json()["detail"].lower()
        assert VALID_KEY not in res.text

    def test_ingest_wrong_key_rejected(self, client):
        res = client.post(
            "/api/scraper/ingest",
            json={
                "company_symbol": "1111",
                "company_name_en": "T",
                "reports": [],
            },
            headers={"X-Internal-Key": "wrong-key"},
        )
        assert res.status_code == 403
        assert VALID_KEY not in res.text
        assert "wrong-key" not in res.text

    def test_ingest_valid_key_accepted(self, client):
        with patch("app.api.routes.scraper.pg_insert") as ins:
            stmt = MagicMock()
            stmt.on_conflict_do_update.return_value = stmt
            ins.return_value = stmt
            client.app.state.test_db.execute = MagicMock()
            client.app.state.test_db.commit = MagicMock()
            res = client.post(
                "/api/scraper/ingest",
                json={
                    "company_symbol": "1111",
                    "company_name_en": "Test Co",
                    "reports": [],
                },
                headers={"X-Internal-Key": VALID_KEY},
            )
        assert res.status_code == 200, res.text
        assert VALID_KEY not in res.text

    def test_session_cookie_cannot_bypass(self, client):
        res = client.post(
            "/api/scraper/ingest",
            json={
                "company_symbol": "1111",
                "company_name_en": "T",
                "reports": [],
            },
            headers={
                "Cookie": "session_token=fake.jwt.token; refresh_token=fake.refresh",
            },
        )
        assert res.status_code == 401

    def test_delete_filing_no_key_rejected(self, client):
        res = client.delete("/api/reports/admin/1111/1")
        assert res.status_code == 401

    def test_delete_filing_wrong_key_rejected(self, client):
        res = client.delete(
            "/api/reports/admin/1111/1",
            headers={"X-Internal-Key": "bad"},
        )
        assert res.status_code == 403

    def test_delete_filing_valid_key_reaches_handler(self, client):
        q = MagicMock()
        q.filter.return_value.first.return_value = None
        client.app.state.test_db.query.return_value = q
        res = client.delete(
            "/api/reports/admin/1111/99",
            headers={"X-Internal-Key": VALID_KEY},
        )
        # Auth passed; filing missing → 404 (not 401/403)
        assert res.status_code == 404
        assert VALID_KEY not in res.text

    def test_official_ingest_requires_key(self, client):
        res = client.post(
            "/api/ingest/official-reports",
            json={"symbol": "1111", "language": "en", "data": {}},
        )
        assert res.status_code == 401


class TestVerifyInternalKeyUnit:
    def test_missing_key_401(self, monkeypatch):
        monkeypatch.setattr("app.core.security.settings.INTERNAL_API_KEY", VALID_KEY)
        with pytest.raises(Exception) as exc:
            verify_internal_key(None)
        assert exc.value.status_code == 401

    def test_wrong_key_403(self, monkeypatch):
        monkeypatch.setattr("app.core.security.settings.INTERNAL_API_KEY", VALID_KEY)
        with pytest.raises(Exception) as exc:
            verify_internal_key("nope")
        assert exc.value.status_code == 403

    def test_valid_key_ok(self, monkeypatch):
        monkeypatch.setattr("app.core.security.settings.INTERNAL_API_KEY", VALID_KEY)
        assert verify_internal_key(VALID_KEY) is True

    def test_key_not_in_error_detail(self, monkeypatch, caplog):
        monkeypatch.setattr("app.core.security.settings.INTERNAL_API_KEY", VALID_KEY)
        with pytest.raises(Exception) as exc:
            verify_internal_key("secret-attempt-value")
        assert VALID_KEY not in str(exc.value.detail)
        assert "secret-attempt-value" not in str(exc.value.detail)
        assert VALID_KEY not in caplog.text
        assert "secret-attempt-value" not in caplog.text
