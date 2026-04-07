"""Nativer VW WeConnect API-Client auf Basis von aiohttp.

Ersetzt die weconnect-Bibliothek, die wegen requests-Versionskonflikten
mit aktuellen Home-Assistant-Versionen nicht mehr installierbar ist.
"""
from __future__ import annotations

import logging
import re
import secrets
import time
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import parse_qsl, urljoin

import aiohttp

_LOGGER = logging.getLogger(__name__)

# --- Konfiguration ---
_CLIENT_ID = "a24fba63-34b3-4d43-b181-942111e6bda8@apps_vw-dilab_com"
_REDIRECT_URI = "weconnect://authenticated"

_AUTH_URL = "https://emea.bff.cariad.digital/user-login/v1/authorize"
_TOKEN_URL = "https://emea.bff.cariad.digital/user-login/login/v1"
_REFRESH_URL = "https://emea.bff.cariad.digital/login/v1/idk/token"
_VEHICLES_URL = "https://emea.bff.cariad.digital/vehicle/v1/vehicles"
_IMAGES_URL = "https://emea.bff.cariad.digital/media/v2/vehicle-images/{vin}?resolution=2x"
_IDENTITY_HOST = "https://identity.vwgroup.io"
_LOGIN_URL = f"{_IDENTITY_HOST}/u/login"

_WEB_HEADERS: dict[str, str] = {
    "User-Agent": "Volkswagen/3.51.1-android/14",
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
    "User-Agent": "Volkswagen/3.51.1-android/14",
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

    Authentifizierung erfolgt über den EMEA-BFF-Endpunkt von Volkswagen/Cariad.
    Tokens werden im Objekt gespeichert und bei Ablauf automatisch erneuert.
    """

    def __init__(self, api_session: aiohttp.ClientSession) -> None:
        """Initialisiert den Client mit einer aiohttp-Session für API-Aufrufe."""
        self._session = api_session
        self._access_token: Optional[str] = None
        self._refresh_token: Optional[str] = None
        self._id_token: Optional[str] = None
        self._expires_at: float = 0.0

    # ------------------------------------------------------------------ #
    # Öffentliche Schnittstelle                                            #
    # ------------------------------------------------------------------ #

    async def login(self, username: str, password: str) -> None:
        """Führt den vollständigen OAuth2-Login durch."""
        async with aiohttp.ClientSession(
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        ) as web_session:
            callback_url = await self._web_auth(web_session, username, password)
        await self._exchange_tokens(callback_url)

    async def get_vehicles(self) -> list[dict]:
        """Liefert die Fahrzeugliste aus der WeConnect API."""
        await self._ensure_token()
        try:
            async with self._session.get(_VEHICLES_URL, headers=self._api_headers()) as resp:
                if resp.status == 401:
                    await self._do_refresh()
                    async with self._session.get(_VEHICLES_URL, headers=self._api_headers()) as r2:
                        r2.raise_for_status()
                        data = await r2.json(content_type=None)
                else:
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
        """Stellt sicher, dass ein gültiges Token vorhanden ist."""
        if self._token_valid:
            return
        if self._refresh_token:
            await self._do_refresh()
        else:
            raise WeConnectAuthError("Kein gültiges Token vorhanden – erneutes Login erforderlich")

    async def _do_refresh(self) -> None:
        """Erneuert das Access-Token über das Refresh-Token."""
        if not self._refresh_token:
            raise WeConnectAuthError("Kein Refresh-Token vorhanden")
        headers = {
            "Accept-Encoding": "gzip, deflate, br",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Volkswagen/3.51.1-android/14",
            "x-android-package-name": "com.volkswagen.weconnect",
        }
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
            "client_id": _CLIENT_ID,
        }
        try:
            async with self._session.post(_REFRESH_URL, data=data, headers=headers) as resp:
                if resp.status == 401:
                    self._access_token = None
                    self._refresh_token = None
                    raise WeConnectAuthError("Token-Erneuerung fehlgeschlagen (401)")
                if resp.status != 200:
                    raise WeConnectConnectionError(f"Token-Erneuerung fehlgeschlagen: HTTP {resp.status}")
                token_data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise WeConnectConnectionError(f"Verbindungsfehler bei Token-Erneuerung: {err}") from err

        self._store_tokens(token_data, keep_refresh=True)

    async def _web_auth(
        self, session: aiohttp.ClientSession, username: str, password: str
    ) -> str:
        """Führt den Browser-basierten OAuth2-Flow durch und gibt die Callback-URL zurück."""
        nonce = secrets.token_hex(16)
        start_url = f"{_AUTH_URL}?redirect_uri={_REDIRECT_URI}&nonce={nonce}"

        # Zu Login-Seite navigieren (Redirects manuell folgen)
        url = start_url
        html_content: Optional[str] = None
        for _ in range(10):
            if url.startswith("weconnect://"):
                return url
            try:
                async with session.get(url, headers=_WEB_HEADERS, allow_redirects=False) as resp:
                    if resp.status == 200:
                        html_content = await resp.text()
                        break
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        if location.startswith("weconnect://"):
                            return location
                        url = location if location.startswith("http") else urljoin(url, location)
                    else:
                        raise WeConnectConnectionError(f"Auth-Seite nicht erreichbar: HTTP {resp.status}")
            except aiohttp.ClientError as err:
                raise WeConnectConnectionError(f"Verbindungsfehler: {err}") from err

        if not html_content:
            raise WeConnectAuthError("Login-Seite konnte nicht geladen werden")

        # Legacy-Flow (emailPasswordForm-Formular vorhanden)?
        form_parser = _FormParser("emailPasswordForm")
        form_parser.feed(html_content)
        if form_parser.target and "email" in form_parser.data:
            _LOGGER.debug("Legacy-Auth-Flow wird verwendet")
            return await self._legacy_auth(session, form_parser, username, password)

        # Neuer Flow: state aus HTML extrahieren
        _LOGGER.debug("Neuer Auth-Flow wird verwendet")
        return await self._new_auth(session, html_content, username, password)

    async def _new_auth(
        self,
        session: aiohttp.ClientSession,
        html: str,
        username: str,
        password: str,
    ) -> str:
        """Neuer VW-Auth-Flow: POST auf /u/login mit state-Parameter."""
        match = re.search(r'<input[^>]*name="state"[^>]*value="([^"]*)"', html)
        if not match:
            raise WeConnectAuthError("state-Token nicht in Login-Seite gefunden")
        state = match.group(1)

        login_url = f"{_LOGIN_URL}?state={state}"
        form = aiohttp.FormData()
        form.add_field("username", username)
        form.add_field("password", password)
        form.add_field("state", state)

        try:
            async with session.post(login_url, data=form, headers=_WEB_HEADERS, allow_redirects=False) as resp:
                if resp.status not in (301, 302, 303):
                    raise WeConnectAuthError(f"Login fehlgeschlagen: HTTP {resp.status}")
                redirect_url = resp.headers.get("Location", "")
        except aiohttp.ClientError as err:
            raise WeConnectConnectionError(f"Verbindungsfehler beim Login: {err}") from err

        return await self._follow_auth_redirects(session, redirect_url)

    async def _legacy_auth(
        self,
        session: aiohttp.ClientSession,
        email_form: _FormParser,
        username: str,
        password: str,
    ) -> str:
        """Legacy VW-Auth-Flow mit emailPasswordForm."""
        email_form.data["email"] = username
        target_url = urljoin(_IDENTITY_HOST, email_form.target)

        # Schritt 1: E-Mail-Formular absenden → Passwort-Formular
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
        login_url = f"{_IDENTITY_HOST}/signin-service/v1/{_CLIENT_ID}/{pwd_target}"
        try:
            async with session.post(login_url, data=pwd_data, headers=_WEB_HEADERS, allow_redirects=False) as resp:
                if resp.status not in (301, 302, 303):
                    raise WeConnectAuthError(f"Passwort-Formular fehlgeschlagen: HTTP {resp.status}")
                location = resp.headers.get("Location", "")
                params = dict(parse_qsl(location.split("?", 1)[1] if "?" in location else ""))
                if "error" in params:
                    raise WeConnectAuthError(f"Login-Fehler: {params['error']}")
                redirect_url = location
        except aiohttp.ClientError as err:
            raise WeConnectConnectionError(f"Verbindungsfehler: {err}") from err

        return await self._follow_auth_redirects(session, redirect_url)

    async def _follow_auth_redirects(
        self, session: aiohttp.ClientSession, start_url: str
    ) -> str:
        """Folgt HTTP-Redirects bis zur weconnect://authenticated-URL."""
        url = start_url
        for _ in range(15):
            if url.startswith("weconnect://authenticated"):
                return url
            if url.startswith("weconnect://"):
                raise WeConnectAuthError(f"Unerwartete Callback-URL: {url}")

            abs_url = url if url.startswith("http") else urljoin(_IDENTITY_HOST, url)
            try:
                async with session.get(abs_url, headers=_WEB_HEADERS, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        if location.startswith("weconnect://authenticated"):
                            return location
                        if location.startswith("weconnect://"):
                            raise WeConnectAuthError(f"Unerwartete Callback-URL: {location}")
                        url = location
                    else:
                        raise WeConnectAuthError(f"Unerwarteter Status bei Redirect: HTTP {resp.status}")
            except aiohttp.ClientError as err:
                raise WeConnectConnectionError(f"Verbindungsfehler: {err}") from err

        raise WeConnectAuthError("Zu viele Redirects bei der Authentifizierung")

    async def _exchange_tokens(self, callback_url: str) -> None:
        """Tauscht die Callback-URL gegen finale Access/Refresh-Tokens."""
        if "#" in callback_url:
            fragment = callback_url.split("#", 1)[1]
        elif "?" in callback_url:
            fragment = callback_url.split("?", 1)[1]
        else:
            raise WeConnectAuthError("Keine Tokens in Callback-URL gefunden")

        params = dict(parse_qsl(fragment))
        required = ("state", "id_token", "access_token", "code")
        missing = [k for k in required if k not in params]
        if missing:
            raise WeConnectAuthError(f"Fehlende Token-Parameter in Callback-URL: {missing}")

        body = {
            "state": params["state"],
            "id_token": params["id_token"],
            "redirect_uri": _REDIRECT_URI,
            "region": "emea",
            "access_token": params["access_token"],
            "authorizationCode": params["code"],
        }
        headers = {
            **_API_HEADERS,
            "accept": "application/json",
            "Authorization": f"Bearer {params['id_token']}",
        }

        try:
            async with self._session.post(_TOKEN_URL, json=body, headers=headers) as resp:
                if resp.status != 200:
                    raise WeConnectAuthError(f"Token-Austausch fehlgeschlagen: HTTP {resp.status}")
                data = await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise WeConnectConnectionError(f"Verbindungsfehler beim Token-Austausch: {err}") from err

        self._store_tokens(data, keep_refresh=False)
        if not self._access_token:
            raise WeConnectAuthError("Kein Access-Token in der Token-Antwort")

    def _store_tokens(self, data: dict, *, keep_refresh: bool) -> None:
        """Speichert Tokens aus einer API-Antwort."""
        self._access_token = data.get("accessToken") or data.get("access_token")
        self._id_token = data.get("idToken") or data.get("id_token")
        new_refresh = data.get("refreshToken") or data.get("refresh_token")
        if new_refresh:
            self._refresh_token = new_refresh
        elif not keep_refresh:
            self._refresh_token = None
        expires_in = float(data.get("expiresIn") or data.get("expires_in") or 3600)
        self._expires_at = time.monotonic() + expires_in


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
