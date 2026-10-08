from __future__ import annotations

import json
import time
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa

from task_auth import AuthConfig, AuthError, FLOW_COOKIE, SESSION_COOKIE, TaskAuth
from test_briefing_state import FakeBlob, store_for


@pytest.fixture
def identity():
    config = AuthConfig(
        origin="https://synthetic.example",
        tenant_id="11111111-1111-4111-8111-111111111111",
        client_id="22222222-2222-4222-8222-222222222222",
        owner_id="33333333-3333-4333-8333-333333333333",
        client_secret="synthetic-credential",
        cookie_key=Fernet.generate_key().decode(),
    )
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    jwk.update(kid="synthetic", use="sig", alg="RS256", issuer=config.issuer)
    metadata = {
        "issuer": config.issuer,
        "authorization_endpoint": config.authority + "/oauth2/v2.0/authorize",
        "token_endpoint": config.authority + "/oauth2/v2.0/token",
        "jwks_uri": config.authority + "/discovery/v2.0/keys",
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "response_types_supported": ["code"],
        "subject_types_supported": ["pairwise"],
        "id_token_signing_alg_values_supported": ["RS256"],
    }
    now = int(time.time())
    claims = {
        "iss": config.issuer, "aud": config.client_id, "tid": config.tenant_id, "oid": config.owner_id,
        "sub": "synthetic-subject", "idp": "live.com", "exp": now + 3600, "iat": now, "nbf": now,
        "nonce": "synthetic-nonce",
    }
    calls = []

    def token(**changes):
        return jwt.encode({**claims, **changes}, private, algorithm="RS256", headers={"kid": "synthetic"})

    def provider(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={
                "token_type": "Bearer", "expires_in": 3600, "scope": "openid profile",
                "id_token": token(), "access_token": "synthetic-access-not-for-storage",
            })
        return httpx.Response(200, json=metadata if "openid-configuration" in request.url.path else {"keys": [jwk]})

    blob = FakeBlob()
    auth = TaskAuth(config, store=store_for(blob), client=httpx.Client(transport=httpx.MockTransport(provider)))
    return auth, config, claims, calls, token, blob


def cookie_pair(header, name):
    parsed = SimpleCookie()
    parsed.load(header)
    return name + "=" + parsed[name].value


def test_actual_msal_code_flow_uses_pkce_without_graph_or_offline_scopes(identity):
    auth, config, _, calls, _, _ = identity
    uri, header = auth.login()
    query = parse_qs(urlsplit(uri).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [config.redirect_uri]
    assert query["response_mode"] == ["form_post"]
    assert set(query["scope"][0].split()) == {"openid", "profile"}
    assert "HttpOnly" in header and "Secure" in header and "SameSite=None" in header
    assert all(request.url.host == "login.microsoftonline.com" for request in calls)


def test_real_msal_callback_validates_signed_owner_and_keeps_tokens_out_of_cookies_and_store(identity):
    auth, _, claims, _, _, blob = identity
    uri, flow_cookie = auth.login()
    query = parse_qs(urlsplit(uri).query)
    claims["nonce"] = query["nonce"][0]
    result = auth.callback(cookie_pair(flow_cookie, FLOW_COOKIE), {"state": query["state"][0], "code": "synthetic-code"})
    session = auth.authenticate(cookie_pair(result, SESSION_COOKIE))
    assert session["idp"] == "live.com"
    assert "SameSite=Lax" in result and "HttpOnly" in result and "Secure" in result
    assert "access_token" not in session and "id_token" not in session
    assert "synthetic-access-not-for-storage" not in json.dumps(blob.saved())
    assert "synthetic-credential" not in json.dumps(blob.saved())
    with pytest.raises(AuthError, match="auth_flow_replayed"):
        auth.callback(cookie_pair(flow_cookie, FLOW_COOKIE), {"state": query["state"][0], "code": "synthetic-code"})
    auth.csrf(session, origin=auth.config.origin, token=session["csrf"])
    auth.logout(session)
    with pytest.raises(AuthError, match="authentication_required"):
        auth.authenticate(cookie_pair(result, SESSION_COOKIE))


@pytest.mark.parametrize("changes", [
    {"oid": "44444444-4444-4444-8444-444444444444"},
    {"tid": "44444444-4444-4444-8444-444444444444"},
    {"idp": "https://sts.windows.net/work-tenant/"},
    {"azp": "44444444-4444-4444-8444-444444444444"},
    {"nonce": "other-nonce"},
])
def test_authentication_is_not_owner_authorization(identity, changes):
    auth, _, _, _, token, _ = identity
    with pytest.raises(AuthError, match="owner_not_authorized"):
        auth._claims(token(**changes), "synthetic-nonce")


@pytest.mark.parametrize("changes", [
    {"iss": "https://login.microsoftonline.com/other/v2.0"},
    {"aud": "another-application"},
    {"aud": ["another-application"]},
    {"exp": int(time.time()) - 120},
    {"nbf": int(time.time()) + 300},
    {"iat": int(time.time()) + 300},
])
def test_signed_but_invalid_issuer_audience_or_lifetime_is_refused(identity, changes):
    auth, _, _, _, token, _ = identity
    with pytest.raises(AuthError, match="id_token_invalid"):
        auth._claims(token(**changes), "synthetic-nonce")


def test_forged_unsigned_and_wrong_key_tokens_are_not_trusted(identity):
    auth, _, claims, _, _, _ = identity
    unsigned = jwt.encode(claims, None, algorithm="none", headers={"kid": "synthetic"})
    with pytest.raises(AuthError, match="id_token_invalid"):
        auth._claims(unsigned, "synthetic-nonce")
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong = jwt.encode(claims, other, algorithm="RS256", headers={"kid": "synthetic"})
    with pytest.raises(AuthError, match="id_token_invalid"):
        auth._claims(wrong, "synthetic-nonce")


def test_state_csrf_and_untrusted_proxy_headers_do_not_authenticate(identity):
    auth, _, _, _, _, _ = identity
    uri, header = auth.login()
    with pytest.raises(AuthError, match="auth_state_invalid"):
        auth.callback(cookie_pair(header, FLOW_COOKIE), {"state": "forged", "code": "synthetic"})
    with pytest.raises(AuthError, match="csrf_rejected"):
        auth.csrf({"csrf": "expected"}, origin="https://attacker.example", token="expected")
    with pytest.raises(AuthError, match="csrf_rejected"):
        auth.csrf({"csrf": "expected"}, origin=auth.config.origin, token="wrong")
    with pytest.raises(AuthError, match="secure_origin_required"):
        auth.check_transport("http://synthetic.example/api/tasks/api/session")
    with pytest.raises(AuthError, match="secure_origin_required"):
        auth.check_transport("https://attacker.example/api/tasks/api/session")
    with pytest.raises(AuthError):
        auth.authenticate("")


def test_configuration_has_no_first_visitor_or_local_demo_fallback():
    with pytest.raises(AuthError, match="auth_unconfigured"):
        AuthConfig.load({})
