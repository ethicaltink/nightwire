from nightwire import security


def test_session_is_false_before_signing_in(client):
    assert client.get("/api/auth/session").json() == {"authenticated": False}


def test_login_sets_a_session_cookie(client, password):
    response = client.post("/api/auth/login", json={"password": password})
    assert response.status_code == 200
    assert security.COOKIE_NAME in response.cookies
    assert client.get("/api/auth/session").json()["authenticated"] is True


def test_wrong_password_is_rejected(client):
    response = client.post("/api/auth/login", json={"password": "nope"})
    assert response.status_code == 401
    assert security.COOKIE_NAME not in response.cookies


def test_empty_password_is_rejected(client):
    assert client.post("/api/auth/login", json={}).status_code == 401


def test_logout_clears_the_session(signed_in):
    assert signed_in.post("/api/auth/logout").status_code == 200
    assert signed_in.get("/api/auth/session").json()["authenticated"] is False


def test_cookie_is_httponly_and_lax(client, password):
    response = client.post("/api/auth/login", json={"password": password})
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=lax" in header
    # No Secure flag: plain HTTP on the LAN would never send it back.
    assert "secure" not in header


def test_a_tampered_cookie_is_not_a_session(client):
    client.cookies.set(security.COOKIE_NAME, "v1.9999999999.forged")
    assert client.get("/api/auth/session").json()["authenticated"] is False


def test_gated_api_returns_401_without_a_session(client):
    # A route that does not exist yet still proves the gate runs first: the
    # middleware answers before routing, so this must be 401 and not 404.
    response = client.get("/api/cameras")
    assert response.status_code == 401
    assert response.json()["error"] == "Not signed in."


def test_gated_api_passes_with_a_session(signed_in):
    # A signed-in request succeeds. What
    # matters here is only that the gate no longer refuses it.
    response = signed_in.get("/api/cameras")
    assert response.status_code == 200
    assert "cameras" in response.json()


def test_login_is_rate_limited(client, password):
    app = client.app
    app.state.login_limiter.min_delay_seconds = 0  # keep the test quick
    for _ in range(5):
        assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    blocked = client.post("/api/auth/login", json={"password": "nope"})
    assert blocked.status_code in (401, 429)
    assert "Retry-After" in blocked.headers
    # And a correct password does not bypass the block.
    assert client.post("/api/auth/login", json={"password": password}).status_code == 429


def test_every_attempt_pays_a_fixed_delay(client):
    # The delay is configurable but must not default to zero, or a shared
    # password on the LAN is brute-forceable at wire speed.
    assert client.app.state.login_limiter.min_delay_seconds >= 1.0


def test_spa_shell_is_public_so_the_login_page_can_load(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_unknown_api_path_is_404_json_not_the_html_shell(signed_in):
    response = signed_in.get("/api/nope")
    assert response.status_code == 404
    assert response.json()["error"] == "Not found."


def test_deep_links_get_the_spa_shell(client):
    # FastAPI must hand /playback/front?t=... to the client-side router.
    response = client.get("/playback/front?t=2026-10-27T14:32:05")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


# ------------- revocation, from finding 4 of the 2026-10-29 security review


def test_logout_invalidates_a_copy_of_the_cookie(client, password):
    """The point of finding 4: logout used to clear the browser's copy only.

    A cookie captured before logout stayed valid until it expired, because
    the session was an HMAC-signed timestamp with nothing to revoke.
    """
    client.post("/api/auth/login", json={"password": password})
    stolen = client.cookies[security.COOKIE_NAME]
    assert client.get("/api/cameras").status_code == 200

    client.post("/api/auth/logout")

    client.cookies.set(security.COOKIE_NAME, stolen)
    assert client.get("/api/cameras").status_code == 401
    assert client.get("/api/auth/session").json()["authenticated"] is False


def test_logout_without_a_session_is_still_fine(client):
    assert client.post("/api/auth/logout").status_code == 200


def test_a_changed_password_invalidates_an_issued_session(settings, recordings, password):
    """Changing NVR_PASSWORD used to leave every issued session working."""
    from fastapi.testclient import TestClient

    from nightwire import health, recorders
    from nightwire.main import create_app

    health.reset()
    recorders.reset()
    app = create_app(settings)
    with TestClient(app) as first:
        first.post("/api/auth/login", json={"password": password})
        cookie = first.cookies[security.COOKIE_NAME]
        assert first.get("/api/cameras").status_code == 200

    # Same config directory, so the same database: only the password changes,
    # exactly as it would on a restart with a new NVR_PASSWORD. Settings are
    # frozen, so this is a replacement rather than a mutation.
    import dataclasses

    changed = dataclasses.replace(settings, password="a-different-password")
    health.reset()
    recorders.reset()
    with TestClient(create_app(changed)) as second:
        second.cookies.set(security.COOKIE_NAME, cookie)
        assert second.get("/api/cameras").status_code == 401
    health.reset()
    recorders.reset()


def test_a_restart_keeps_the_station_signed_in(settings, recordings, password):
    """The property the original stateless session existed for, kept."""
    from fastapi.testclient import TestClient

    from nightwire import health, recorders
    from nightwire.main import create_app

    health.reset()
    recorders.reset()
    app = create_app(settings)
    with TestClient(app) as first:
        first.post("/api/auth/login", json={"password": password})
        cookie = first.cookies[security.COOKIE_NAME]

    health.reset()
    recorders.reset()
    with TestClient(create_app(settings)) as second:
        second.cookies.set(security.COOKIE_NAME, cookie)
        assert second.get("/api/cameras").status_code == 200
    health.reset()
    recorders.reset()


def test_two_sessions_are_independent(settings, recordings, password):
    from fastapi.testclient import TestClient

    from nightwire import health, recorders
    from nightwire.main import create_app

    health.reset()
    recorders.reset()
    app = create_app(settings)
    with TestClient(app) as station, TestClient(app) as laptop:
        station.post("/api/auth/login", json={"password": password})
        laptop.post("/api/auth/login", json={"password": password})
        laptop.post("/api/auth/logout")
        assert station.get("/api/cameras").status_code == 200
        assert laptop.get("/api/cameras").status_code == 401
    health.reset()
    recorders.reset()


def test_a_random_token_is_not_a_session(client):
    client.cookies.set(security.COOKIE_NAME, security.new_token())
    assert client.get("/api/cameras").status_code == 401


def test_the_session_payload_reports_when_it_was_issued(signed_in):
    payload = signed_in.get("/api/auth/session").json()
    assert payload["authenticated"] is True
    assert payload["issued_at"] > 1_700_000_000
