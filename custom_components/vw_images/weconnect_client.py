"""Nativer VW WeConnect API-Client auf Basis von aiohttp.

Authentifizierung über OIDC Hybrid Flow (response_type=code id_token token).
Access- und ID-Token kommen direkt in der Callback-URL – kein separater
Token-Tausch gegen den CARIAD BFF nötig (der Token-Endpoint gibt seit
Ende Mai 2026 403/400 zurück).

Bekannte Einschränkung: Kein Refresh-Token. Nach Ablauf des Access-Tokens
(~2 h) ist ein erneutes Login erforderlich.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qs, parse_qsl, urljoin, urlparse

import aiohttp

_LOGGER = logging.getLogger(__name__)

# --- Konfiguration ---
_CLIENT_ID = "a24fba63-34b3-4d43-b181-942111e6bda8@apps_vw-dilab_com"
_CLIENT_SCOPE = "openid profile badge cars dealers vin offline_access"
_REDIRECT_URI = "weconnect://authenticated"

# OIDC Discovery liefert den tatsächlichen authorization_endpoint dynamisch.
# Hinweis: /user-login/v1/authorize (alt) + /user-login/login/v1 (Token-Tausch)
# geben seit Ende Mai 2026 403 zurück → ersetzt durch Hybrid Flow.
_OPENID_CONFIG_URL = "https://emea.bff.cariad.digital/auth/v1/idk/oidc/openid-configuration"
_VEHICLES_URL = "https://emea.bff.cariad.digital/vehicle/v1/vehicles"
_IMAGES_URL = "https://emea.bff.cariad.digital/media/v2/vehicle-images/{vin}?resolution=2x"
_IDENTITY_HOST = "https://identity.vwgroup.io"

_USER_AGENT = "Volkswagen/3.61.0-android/14"

_WEB_HEADERS: dict[str, str] = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "x-requested-with": "de.volkswagen.carnet.eu.eremote",
    "x-android-package-name": "com.volkswagen.weconnect",
}

_API_HEADERS: dict[str, str] = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Content-Version": "1",
    "x-newrelic-id": "VgAEWV9QDRAEXFlRAAYPUA==",
    "User-Agent": _USER_AGENT,
    "Accept-Language": "de-de",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "x-android-package-name": "com.volkswagen.weconnect",
}


# --- Fehlerklassen ---

class WeConnectAuthError(Exception):
    """Authentifizierungsfehler."""


class WeConnectConnectionError(Exception):
    """Verbindungsfehler."""


# --- HTML-Form-Parser (stdlib, keine externe Abhängigkeit) ---

class _FormParser(HTMLParser):
    """Parst ein einzelnes HTML-Formular anhand seiner ID."""

    def __init__(self, form_id: str) -> None:
        super().__init__()
        self._form_id = form_id
        self._inside = False
        self.target: Optional[str] = None
        self.data: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list) -> None:
        d = dict(attrs)
        if tag == "form" and d.get("id") == self._form_id:
            self._inside = True
            self.target = d.get("action")
        elif self._inside and tag == "input":
            name = d.get("name")
            if name:
                self.data[name] = d.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._inside = False


# --- Haupt-API-Client ---

class WeConnectAPIClient:
    """Nativer VW WeConnect API-Client.

    Authentifizierung über OIDC Hybrid Flow gegen identity.vwgroup.io (Auth0).
    Tokens werden im Objekt gespeichert; bei Ablauf ist ein erneutes Login
    erforderlich (kein Refresh-Token verfügbar).
    """

    def __init__(self, api_session: aiohttp.ClientSession) -> None:
        """Initialisiert den Client mit einer aiohttp-Session für API-Aufrufe."""
        self._session = api_session
        self._access_token: Optional[str] = None
        self._id_token: Optional[str] = None
        self._expires_at: float = 0.0

    # ------------------------------------------------------------------ #
    # Öffentliche Schnittstelle                                            #
    # ------------------------------------------------------------------ #

    async def login(self, username: str, password: str) -> None:
        """Führt den vollständigen OIDC-Hybrid-Login durch."""
        # Schritt 1: OpenID-Konfiguration abrufen (liefert authorization_endpoint)
        openid_config = await self._fetch_openid_config()
        authorization_endpoint = openid_config.get("authorization_endpoint")
        issuer = openid_config.get("issuer", _IDENTITY_HOST)
        if not authorization_endpoint:
            raise WeConnectAuthError("authorization_endpoint nicht in OpenID-Config gefunden")

        # Schritt 2: Browser-Auth-Flow → Callback-URL mit Tokens
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        ) as web_session:
            callback_url = await self._web_auth(
                web_session, authorization_endpoint, issuer, username, password
            )

        # Schritt 3: Tokens direkt aus der Callback-URL lesen (kein HTTP-Austausch)
        self._parse_callback_tokens(callback_url)

    async def get_vehicles(self) -> list[dict]:
        """Liefert die Fahrzeugliste aus der WeConnect API."""
        await self._ensure_token()
        try:
            async with self._session.get(_VEHICLES_URL, headers=self._api_headers()) as resp:
                if resp.status == 401:
                    raise WeConnectAuthError("Token abgelaufen (401) – erneutes Login erforderlich")
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise WeConnectConnectionError(f"Fahrzeugliste konnte nicht abgerufen werden: {err}") from err
        return data.get("data", [])

    async def get_vehicle_image_urls(self, vin: str) -> dict[str, str]:
        """Liefert ein Dict {image_id: download_url} für ein Fahrzeug."""
        await self._ensure_token()
        url = _IMAGES_URL.format(vin=vin)
        try:
            async with self._session.get(url, headers=self._api_headers()) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Bilder-Endpunkt für ***%s: HTTP %d", vin[-4:], resp.status)
                    return {}
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            _LOGGER.debug("Bilder-URL-Abruf fehlgeschlagen für ***%s: %s", vin[-4:], err)
            return {}
        return {
            img["id"]: img["url"]
            for img in data.get("data", [])
            if "id" in img and "url" in img
        }

    async def download_image(self, url: str) -> Optional[bytes]:
        """Lädt ein Bild herunter und gibt die rohen Bytes zurück."""
        await self._ensure_token()
        headers = {k: v for k, v in self._api_headers().items() if k != "Content-Type"}
        try:
            async with self._session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.read()
                _LOGGER.debug("Bild-Download fehlgeschlagen: HTTP %d", resp.status)
        except aiohttp.ClientError as err:
            _LOGGER.debug("Bild-Download Verbindungsfehler: %s", err)
        return None

    # ------------------------------------------------------------------ #
    # Authentifizierungs-Internals                                         #
    # ------------------------------------------------------------------ #

    def _api_headers(self) -> dict[str, str]:
        """Gibt API-Header mit Bearer-Token und Trace-ID zurück."""
        trace = secrets.token_hex(16)
        trace_id = f"{trace[:8]}-{trace[8:12]}-{trace[12:16]}-{trace[16:20]}-{trace[20:]}".upper()
        headers = dict(_API_HEADERS)
        headers["weconnect-trace-id"] = trace_id
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        return headers

    @property
    def _token_valid(self) -> bool:
        return bool(self._access_token) and time.monotonic() < self._expires_at - 60

    async def _ensure_token(self) -> None:
        """Stellt sicher, dass ein gültiges Token vorhanden ist.

        Kein Refresh-Token verfügbar (OIDC Hybrid Flow). Bei Ablauf muss
        der Coordinator ein erneutes Login auslösen.
        """
        if self._token_valid:
            return
        raise WeConnectAuthError("Token abgelaufen – erneutes Login erforderlich")

    async def _fetch_openid_config(self) -> dict:
        """Ruft die OIDC-Discovery-Konfiguration ab."""
        try:
            async with self._session.get(_OPENID_CONFIG_URL) as resp:
                if resp.status != 200:
                    raise WeConnectAuthError(
                        f"OpenID-Konfiguration nicht abrufbar: HTTP {resp.status}"
                    )
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise WeConnectConnectionError(
                f"Verbindungsfehler beim Abruf der OpenID-Konfiguration: {err}"
            ) from err

    async def _web_auth(
        self,
        session: aiohttp.ClientSession,
        authorization_endpoint: str,
        issuer: str,
        username: str,
        password: str,
    ) -> str:
        """OIDC Hybrid Flow: liefert die weconnect://authenticated Callback-URL."""
        nonce = secrets.token_hex(16)

        # GET authorization_endpoint → Redirect zur Login-Seite folgen
        params = {
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code id_token token",  # Hybrid Flow
            "client_id": _CLIENT_ID,
            "scope": _CLIENT_SCOPE,
            "nonce": nonce,
        }

        html_content: Optional[str] = None
        url = authorization_endpoint
        for _ in range(10):
            if url.startswith("weconnect://"):
                return url
            try:
                async with session.get(
                    url, headers=_WEB_HEADERS, params=params if url == authorization_endpoint else None,
                    allow_redirects=False
                ) as resp:
                    if resp.status == 200:
                        html_content = await resp.text()
                        break
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        if location.startswith("weconnect://"):
                            return location
                        url = location if location.startswith("http") else urljoin(url, location)
                    else:
                        raise WeConnectConnectionError(
                            f"Auth-Seite nicht erreichbar: HTTP {resp.status}"
                        )
            except aiohttp.ClientError as err:
                raise WeConnectConnectionError(f"Verbindungsfehler: {err}") from err

        if not html_content:
            raise WeConnectAuthError("Login-Seite konnte nicht geladen werden")

        # Legacy-Flow (emailPasswordForm vorhanden)?
        form_parser = _FormParser("emailPasswordForm")
        form_parser.feed(html_content)
        if form_parser.target and "email" in form_parser.data:
            _LOGGER.debug("Legacy-Auth-Flow wird verwendet")
            return await self._legacy_auth(session, form_parser, issuer, username, password)

        # Aktueller Auth0-Flow
        _LOGGER.debug("Auth0-Hybrid-Flow wird verwendet")
        return await self._new_auth(session, html_content, issuer, username, password)

    async def _new_auth(
        self,
        session: aiohttp.ClientSession,
        html: str,
        issuer: str,
        username: str,
        password: str,
    ) -> str:
        """Auth0 Universal Login: POST auf /u/login mit state + action=default."""
        match = re.search(r'<input[^>]*name="state"[^>]*value="([^"]*)"', html)
        if not match:
            raise WeConnectAuthError("state-Token nicht in Login-Seite gefunden")
        state = match.group(1)

        login_url = f"{issuer}/u/login?state={state}"
        form = aiohttp.FormData()
        form.add_field("username", username)
        form.add_field("password", password)
        form.add_field("state", state)
        form.add_field("action", "default")  # Pflichtfeld im Auth0 Universal Login

        try:
            async with session.post(
                login_url, data=form, headers=_WEB_HEADERS, allow_redirects=False
            ) as resp:
                if resp.status not in (301, 302, 303):
                    raise WeConnectAuthError(f"Login fehlgeschlagen: HTTP {resp.status}")
                redirect_url = resp.headers.get("Location", "")
        except aiohttp.ClientError as err:
            raise WeConnectConnectionError(f"Verbindungsfehler beim Login: {err}") from err

        return await self._follow_auth_redirects(session, redirect_url, issuer)

    async def _legacy_auth(
        self,
        session: aiohttp.ClientSession,
        email_form: _FormParser,
        issuer: str,
        username: str,
        password: str,
    ) -> str:
        """Legacy VW-Auth-Flow mit emailPasswordForm."""
        email_form.data["email"] = username
        target_url = urljoin(issuer, email_form.target)

        # Schritt 1: E-Mail-Formular → Passwort-Formular
        try:
            async with session.post(target_url, data=email_form.data, headers=_WEB_HEADERS) as resp:
                if resp.status != 200:
                    raise WeConnectAuthError(f"E-Mail-Formular fehlgeschlagen: HTTP {resp.status}")
                pwd_html = await resp.text()
        except aiohttp.ClientError as err:
            raise WeConnectConnectionError(f"Verbindungsfehler: {err}") from err

        pwd_data, pwd_target = _parse_script_form(
            pwd_html, "postAction", ["relayState", "hmac", "_csrf"]
        )
        if not pwd_target:
            raise WeConnectAuthError("Passwort-Formular nicht gefunden")

        # Schritt 2: Passwort absenden
        pwd_data["email"] = username
        pwd_data["password"] = password
        login_url = f"{issuer}/signin-service/v1/{_CLIENT_ID}/{pwd_target}"
        try:
            async with session.post(
                login_url, data=pwd_data, headers=_WEB_HEADERS, allow_redirects=False
            ) as resp:
                if resp.status not in (301, 302, 303):
                    raise WeConnectAuthError(f"Passwort-Formular fehlgeschlagen: HTTP {resp.status}")
                location = resp.headers.get("Location", "")
                params = dict(parse_qsl(location.split("?", 1)[1] if "?" in location else ""))
                if "error" in params:
                    raise WeConnectAuthError(f"Login-Fehler: {params['error']}")
                redirect_url = location
        except aiohttp.ClientError as err:
            raise WeConnectConnectionError(f"Verbindungsfehler: {err}") from err

        return await self._follow_auth_redirects(session, redirect_url, issuer)

    async def _follow_auth_redirects(
        self, session: aiohttp.ClientSession, start_url: str, issuer: str
    ) -> str:
        """Folgt HTTP-Redirects bis zur weconnect://authenticated-URL."""
        url = start_url
        for _ in range(15):
            if url.startswith("weconnect://authenticated"):
                return url
            if url.startswith("weconnect://"):
                raise WeConnectAuthError(f"Unerwartete Callback-URL: {url}")

            abs_url = url if url.startswith("http") else urljoin(issuer, url)
            try:
                async with session.get(
                    abs_url, headers=_WEB_HEADERS, allow_redirects=False
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        if location.startswith("weconnect://authenticated"):
                            return location
                        if location.startswith("weconnect://"):
                            raise WeConnectAuthError(f"Unerwartete Callback-URL: {location}")
                        url = location
                    else:
                        raise WeConnectAuthError(
                            f"Unerwarteter Status bei Redirect: HTTP {resp.status}"
                        )
            except aiohttp.ClientError as err:
                raise WeConnectConnectionError(f"Verbindungsfehler: {err}") from err

        raise WeConnectAuthError("Zu viele Redirects bei der Authentifizierung")

    def _parse_callback_tokens(self, callback_url: str) -> None:
        """Liest Access- und ID-Token direkt aus der Hybrid-Flow-Callback-URL.

        Der OIDC Hybrid Flow (response_type=code id_token token) liefert
        access_token und id_token sowohl im Query-String als auch im Fragment.
        Kein separater Token-Tausch gegen den CARIAD BFF nötig.
        """
        parsed = urlparse(callback_url)
        # Query-String und Fragment zusammenführen (Auth0 nutzt je nach Konfiguration beides)
        all_params: dict[str, list[str]] = {}
        if parsed.query:
            all_params.update(parse_qs(parsed.query))
        if parsed.fragment:
            all_params.update(parse_qs(parsed.fragment))

        access_token = (all_params.get("access_token") or [None])[0]
        id_token = (all_params.get("id_token") or [None])[0]

        if not access_token:
            raise WeConnectAuthError(
                "Kein access_token in der Callback-URL – Login fehlgeschlagen"
            )

        self._access_token = access_token
        self._id_token = id_token
        # Kein Refresh-Token im Hybrid Flow; Token-Laufzeit ~2 h (Auth0-Standard)
        expires_in = float((all_params.get("expires_in") or [7200])[0])
        self._expires_at = time.monotonic() + expires_in
        _LOGGER.debug("Login erfolgreich, Token gültig für %.0f s", expires_in)


# --- Hilfsfunktion für Legacy-Script-Form-Parsing ---

def _parse_script_form(html: str, target_field: str, fields: list[str]) -> tuple[dict, Optional[str]]:
    """Parst ein VW-Login-Script-Tag mit templateModel-JSON."""
    import json as _json
    match = re.search(r"templateModel: (.*?),\n", html)
    if not match:
        return {}, None
    try:
        raw = _json.loads(match.group(1))
    except Exception:
        return {}, None
    target = raw.get(target_field)
    result = {k: v for k, v in raw.items() if k in fields}
    csrf_match = re.search(r"csrf_token: '(.*?)'", html)
    if csrf_match:
        result["_csrf"] = csrf_match.group(1)
    return result, target
