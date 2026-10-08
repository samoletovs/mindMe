"""Microsoft OIDC BFF: exact owner authorization, encrypted cookies, no browser tokens."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import msal
from cryptography.fernet import Fernet, InvalidToken

from briefing_state import BriefingStore
from execution_budget import bounded_timeout
from task_state import prune_auth, workspace

FLOW_COOKIE = "__Host-mindme-flow"
SESSION_COOKIE = "__Host-mindme-session"
FLOW_SECONDS = 600
SESSION_SECONDS = 3600
_UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\Z")


class AuthError(RuntimeError):
    def __init__(self, code: str, status: int = 401) -> None:
        super().__init__(code)
        self.status = status


@dataclass(frozen=True)
class AuthConfig:
    origin: str
    tenant_id: str
    client_id: str
    owner_id: str
    client_secret: str
    cookie_key: str

    @classmethod
    def load(cls, env: Mapping[str, str]) -> AuthConfig:
        values = [
            env.get("MINDME_WEB_" + name, "").strip()
            for name in ("ORIGIN", "TENANT_ID", "CLIENT_ID", "OWNER_OBJECT_ID", "CLIENT_SECRET", "COOKIE_KEY")
        ]
        if not all(values):
            raise AuthError("auth_unconfigured", 503)
        config = cls(*values)
        try:
            origin = urlsplit(config.origin)
        except ValueError:
            raise AuthError("auth_configuration_invalid", 503) from None
        if (
            origin.scheme != "https" or not origin.hostname or origin.path
            or origin.query or origin.fragment or origin.username or origin.password
            or origin.netloc != origin.hostname or config.origin != f"https://{origin.hostname}"
            or not all(_UUID.fullmatch(value) for value in (config.tenant_id, config.client_id, config.owner_id))
            or config.tenant_id in {"common", "organizations", "consumers"}
        ):
            raise AuthError("auth_configuration_invalid", 503)
        try:
            Fernet(config.cookie_key.encode("ascii"))
        except (ValueError, UnicodeError):
            raise AuthError("auth_configuration_invalid", 503) from None
        return config

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    @property
    def issuer(self) -> str:
        return self.authority + "/v2.0"

    @property
    def redirect_uri(self) -> str:
        return self.origin + "/api/tasks/auth/callback"


class _IdentityHTTP:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    @staticmethod
    def _url(url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https" or parsed.netloc != "login.microsoftonline.com"
            or parsed.username or parsed.password or parsed.fragment
        ):
            raise AuthError("auth_endpoint_invalid", 503)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self._url(url)
        kwargs.pop("timeout", None)
        return self.client.get(url, **kwargs, follow_redirects=False, timeout=bounded_timeout(10, stages=4))

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self._url(url)
        kwargs.pop("timeout", None)
        return self.client.post(url, **kwargs, follow_redirects=False, timeout=bounded_timeout(10, stages=4))


def cookie_header(name: str, value: str, *, max_age: int, flow: bool = False) -> str:
    cookie: SimpleCookie = SimpleCookie()
    cookie[name] = value
    cookie[name]["path"] = "/"
    cookie[name]["secure"] = True
    cookie[name]["httponly"] = True
    cookie[name]["samesite"] = "None" if flow else "Lax"
    cookie[name]["max-age"] = max_age
    return cookie.output(header="").strip()


def _cookie(header: str, name: str) -> str:
    if len(header) > 12000 or sum(part.strip().startswith(name + "=") for part in header.split(";")) != 1:
        raise AuthError("authentication_required")
    parsed: SimpleCookie = SimpleCookie()
    try:
        parsed.load(header)
    except (CookieError, ValueError, TypeError):
        raise AuthError("authentication_required") from None
    if name not in parsed:
        raise AuthError("authentication_required")
    return parsed[name].value


class TaskAuth:
    def __init__(
        self, config: AuthConfig, *, store: BriefingStore, client: httpx.Client,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config, self.store, self.client, self.clock = config, store, client, clock
        self.cipher = Fernet(config.cookie_key.encode("ascii"))
        for name in ("msal", "httpx", "httpcore"):
            logging.getLogger(name).setLevel(logging.WARNING)

    def check_transport(self, url: str) -> None:
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise AuthError("secure_origin_required", 400) from None
        if (
            parsed.scheme != "https" or f"{parsed.scheme}://{parsed.netloc}" != self.config.origin
            or parsed.username or parsed.password
        ):
            raise AuthError("secure_origin_required", 400)

    def _application(self) -> msal.ConfidentialClientApplication:
        return msal.ConfidentialClientApplication(
            self.config.client_id, client_credential=self.config.client_secret,
            authority=self.config.authority, instance_discovery=False,
            exclude_scopes=["offline_access"], http_client=_IdentityHTTP(self.client),
            enable_pii_log=False,
        )

    def _seal(self, value: dict[str, Any]) -> str:
        encoded = self.cipher.encrypt(json.dumps(value, separators=(",", ":")).encode()).decode("ascii")
        if len(encoded) > 3800:
            raise AuthError("auth_cookie_capacity", 503)
        return encoded

    def _open(self, value: str, ttl: int) -> dict[str, Any]:
        try:
            result = json.loads(self.cipher.decrypt(value.encode("ascii"), ttl=ttl))
        except (InvalidToken, ValueError, UnicodeError):
            raise AuthError("authentication_required") from None
        if not isinstance(result, dict):
            raise AuthError("authentication_required")
        return result

    def login(self) -> tuple[str, str]:
        try:
            flow = self._application().initiate_auth_code_flow(
                scopes=[], redirect_uri=self.config.redirect_uri, response_mode="form_post", prompt="select_account",
            )
        except (ValueError, httpx.HTTPError):
            raise AuthError("identity_provider_unavailable", 503) from None
        uri = flow.pop("auth_uri", None)
        if not isinstance(uri, str):
            raise AuthError("identity_provider_unavailable", 503)
        _IdentityHTTP._url(uri)
        nonce = parse_qs(urlsplit(uri).query).get("nonce", [])
        if len(nonce) != 1 or not flow.get("code_verifier"):
            raise AuthError("auth_flow_invalid", 503)
        token = self._seal({"flow": flow, "nonce": nonce[0], "expires": int(self.clock()) + FLOW_SECONDS})
        return uri, cookie_header(FLOW_COOKIE, token, max_age=FLOW_SECONDS, flow=True)

    def _claims(self, token: object, nonce: str) -> dict[str, Any]:
        if not isinstance(token, str) or not 1 <= len(token) <= 16000:
            raise AuthError("id_token_invalid")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise AuthError("id_token_invalid")
            transport = _IdentityHTTP(self.client)
            discovery = transport.get(self.config.authority + "/v2.0/.well-known/openid-configuration")
            discovery.raise_for_status()
            metadata = discovery.json()
            if not isinstance(metadata, dict) or metadata.get("issuer") != self.config.issuer:
                raise AuthError("identity_issuer_invalid")
            jwks_uri = metadata.get("jwks_uri")
            if not isinstance(jwks_uri, str):
                raise AuthError("identity_keys_unavailable", 503)
            keys = transport.get(jwks_uri)
            keys.raise_for_status()
            body = keys.json()
            if not isinstance(body, dict) or not isinstance(body.get("keys"), list) or len(body["keys"]) > 32:
                raise AuthError("identity_keys_unavailable", 503)
            candidates = [
                key for key in body["keys"]
                if isinstance(key, dict) and key.get("kid") == header["kid"] and key.get("kty") == "RSA"
                and key.get("use", "sig") == "sig" and key.get("alg", "RS256") == "RS256"
                and isinstance(key.get("issuer", self.config.issuer), str)
                and key.get("issuer", self.config.issuer).replace("{tenantid}", self.config.tenant_id) == self.config.issuer
            ]
            if len(candidates) != 1:
                raise AuthError("id_token_invalid")
            claims = jwt.decode(
                token, jwt.PyJWK.from_dict(candidates[0], algorithm="RS256").key,
                algorithms=["RS256"], audience=self.config.client_id, issuer=self.config.issuer,
                leeway=30,
                options={"require": ["exp", "iat", "nbf", "iss", "aud", "tid", "oid", "nonce"],
                         "strict_aud": True},
            )
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise AuthError("id_token_invalid") from None
        except httpx.HTTPError:
            raise AuthError("identity_provider_unavailable", 503) from None
        if (
            claims.get("tid") != self.config.tenant_id or claims.get("oid") != self.config.owner_id
            or claims.get("idp") != "live.com" or claims.get("azp", self.config.client_id) != self.config.client_id
            or not isinstance(claims.get("nonce"), str)
            or not secrets.compare_digest(claims["nonce"], nonce)
        ):
            raise AuthError("owner_not_authorized", 403)
        return claims

    def callback(self, cookie: str, response: dict[str, str]) -> str:
        now = int(self.clock())
        envelope = self._open(_cookie(cookie, FLOW_COOKIE), FLOW_SECONDS)
        if (
            type(envelope.get("expires")) is not int or envelope["expires"] <= now
            or not isinstance(envelope.get("flow"), dict) or not isinstance(envelope.get("nonce"), str)
            or not isinstance(response.get("code"), str) or not isinstance(response.get("state"), str)
        ):
            raise AuthError("auth_flow_invalid")
        flow = envelope["flow"]
        if not isinstance(flow.get("state"), str) or not secrets.compare_digest(response["state"], flow["state"]):
            raise AuthError("auth_state_invalid")
        nonce_hash = hashlib.sha256(envelope["nonce"].encode()).hexdigest()
        if nonce_hash in workspace(self.store.read())["auth_nonces"]:
            raise AuthError("auth_flow_replayed")
        try:
            result = self._application().acquire_token_by_auth_code_flow(flow, response, scopes=[])
        except (ValueError, RuntimeError, httpx.HTTPError):
            raise AuthError("auth_code_invalid") from None
        if not isinstance(result, dict) or result.get("error"):
            raise AuthError("auth_code_invalid")
        claims = self._claims(result.get("id_token"), envelope["nonce"])
        expiry = min(now + SESSION_SECONDS, int(claims["exp"]))
        if expiry <= now:
            raise AuthError("id_token_expired")
        session_id = secrets.token_urlsafe(32)
        session_hash = hashlib.sha256(session_id.encode()).hexdigest()

        def claim(state: dict[str, Any]) -> None:
            prune_auth(state, now)
            data = workspace(state)
            if nonce_hash in data["auth_nonces"]:
                raise AuthError("auth_flow_replayed")
            data["auth_nonces"][nonce_hash] = now + FLOW_SECONDS
            data["auth_sessions"][session_hash] = expiry

        self.store.update(claim)
        value = self._seal({
            "sid": session_id, "csrf": secrets.token_urlsafe(32), "exp": expiry,
            "tid": self.config.tenant_id, "oid": self.config.owner_id, "aud": self.config.client_id,
            "iss": self.config.issuer, "idp": "live.com",
        })
        return cookie_header(SESSION_COOKIE, value, max_age=expiry - now)

    def authenticate(self, cookie: str) -> dict[str, Any]:
        session = self._open(_cookie(cookie, SESSION_COOKIE), SESSION_SECONDS)
        if (
            type(session.get("exp")) is not int or session["exp"] <= self.clock()
            or session.get("tid") != self.config.tenant_id or session.get("oid") != self.config.owner_id
            or session.get("aud") != self.config.client_id or session.get("iss") != self.config.issuer
            or session.get("idp") != "live.com" or not isinstance(session.get("sid"), str)
            or not isinstance(session.get("csrf"), str)
        ):
            raise AuthError("authentication_required")
        session_hash = hashlib.sha256(session["sid"].encode()).hexdigest()
        if workspace(self.store.read())["auth_sessions"].get(session_hash, 0) <= self.clock():
            raise AuthError("authentication_required")
        return session

    def csrf(self, session: dict[str, Any], *, origin: str, token: str) -> None:
        if origin != self.config.origin or not token or not secrets.compare_digest(token, session["csrf"]):
            raise AuthError("csrf_rejected", 403)

    def logout(self, session: dict[str, Any]) -> str:
        key = hashlib.sha256(session["sid"].encode()).hexdigest()
        self.store.update(lambda state: workspace(state)["auth_sessions"].pop(key, None))
        return cookie_header(SESSION_COOKIE, "", max_age=0)
