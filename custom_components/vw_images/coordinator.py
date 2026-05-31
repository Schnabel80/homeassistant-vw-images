"""Daten-Koordinator für die VW Images Integration."""

from __future__ import annotations

import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import MIN_REFRESH_INTERVAL
from .weconnect_client import WeConnectAPIClient, WeConnectAuthError, WeConnectConnectionError

_LOGGER = logging.getLogger(__name__)

# Mapping: WeConnect API-Bild-ID → Integrations-Picture-Key
# car_34view  = 3/4-Ansicht des Fahrzeugs
# car_birdview = Vogelperspektive (für Statusbilder)
_IMAGE_ID_MAP: dict[str, str] = {
    "car_34view": "car",
    "car_birdview": "status",
}


class VWImagesCoordinator(DataUpdateCoordinator):
    """Koordinator für VW Images.

    Kein automatisches Polling – Updates werden nur on-demand
    über async_request_refresh() ausgelöst (Button oder Service-Call).
    Rate-Limiting: Mindestens MIN_REFRESH_INTERVAL Sekunden zwischen Aufrufen.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialisiert den Koordinator ohne update_interval."""
        super().__init__(
            hass,
            _LOGGER,
            name="VW Images",
            # Kein update_interval → nur on-demand
        )
        self.config_entry = entry
        self._client: WeConnectAPIClient | None = None
        self._last_refresh_time: float = 0.0

    async def _async_setup(self) -> None:
        """Einmalige Einrichtung: WeConnect-Login."""
        session = async_get_clientsession(self.hass)
        self._client = WeConnectAPIClient(session)

        username = self.config_entry.data[CONF_USERNAME]
        password = self.config_entry.data[CONF_PASSWORD]

        _LOGGER.info("WeConnect Login wird durchgeführt...")
        try:
            await self._client.login(username, password)
        except WeConnectAuthError as err:
            self._client = None
            raise ConfigEntryAuthFailed(
                "WeConnect-Anmeldung fehlgeschlagen. Bitte Zugangsdaten prüfen."
            ) from err
        except WeConnectConnectionError as err:
            self._client = None
            raise UpdateFailed(f"Netzwerkfehler beim WeConnect-Login: {err}") from err
        _LOGGER.info("WeConnect Login erfolgreich")

    async def _async_update_data(self) -> dict:
        """Fahrzeugdaten und Bilder von WeConnect abrufen (mit Rate-Limiting)."""
        # Rate-Limiting: Mindestabstand zwischen Aufrufen
        now = time.monotonic()
        elapsed = now - self._last_refresh_time
        if self._last_refresh_time > 0 and elapsed < MIN_REFRESH_INTERVAL:
            _LOGGER.debug(
                "Rate-Limit: Nächster Refresh in %d Sekunden möglich",
                int(MIN_REFRESH_INTERVAL - elapsed),
            )
            if self.data is not None:
                return self.data

        try:
            if self._client is None:
                await self._async_setup()

            # Erster Versuch; bei Token-Ablauf einmal still re-loginen
            try:
                return await self._fetch_vehicles()
            except WeConnectAuthError:
                _LOGGER.info("Token abgelaufen – automatisches Re-Login läuft...")
                await self._silent_relogin()
                return await self._fetch_vehicles()

        except ConfigEntryAuthFailed:
            raise
        except WeConnectAuthError as err:
            # Re-Login selbst fehlgeschlagen → echtes Auth-Problem → Nutzer fragen
            _LOGGER.warning("Re-Login fehlgeschlagen: %s", err)
            self._client = None
            raise ConfigEntryAuthFailed(
                "WeConnect-Anmeldung fehlgeschlagen. Bitte Zugangsdaten prüfen."
            ) from err
        except WeConnectConnectionError as err:
            _LOGGER.warning("Verbindungsfehler: %s", err)
            raise UpdateFailed(f"Netzwerkfehler bei WeConnect-Verbindung: {err}") from err
        except Exception as err:
            _LOGGER.warning("WeConnect Update fehlgeschlagen: %s", err)
            self._client = None
            raise UpdateFailed(f"Fehler beim Abrufen der Fahrzeugdaten: {err}") from err

    async def _silent_relogin(self) -> None:
        """Führt ein stilles Re-Login mit gespeicherten Zugangsdaten durch.

        Wird automatisch aufgerufen wenn das Token abgelaufen ist (~2 h).
        Löst keinen HA-Login-Dialog aus – der Nutzer merkt nichts davon.
        """
        username = self.config_entry.data[CONF_USERNAME]
        password = self.config_entry.data[CONF_PASSWORD]
        if self._client is None:
            session = async_get_clientsession(self.hass)
            self._client = WeConnectAPIClient(session)
        await self._client.login(username, password)
        _LOGGER.info("Automatisches Re-Login erfolgreich")

    async def _fetch_vehicles(self) -> dict:
        """Fahrzeugdaten und Bilder abrufen (ohne Fehlerbehandlung)."""
        _LOGGER.debug("Aktualisiere WeConnect Fahrzeugdaten...")
        vehicles_list = await self._client.get_vehicles()
        self._last_refresh_time = time.monotonic()

        vehicles: dict = {}
        for vehicle_data in vehicles_list:
            vin = vehicle_data.get("vin")
            if not vin:
                continue

            model = vehicle_data.get("model") or "VW Fahrzeug"
            nickname = vehicle_data.get("nickname")

            # Bild-URLs abrufen
            image_urls = await self._client.get_vehicle_image_urls(vin)

            # Bilder herunterladen
            image_bytes: dict[str, bytes] = {}
            for api_id, picture_key in _IMAGE_ID_MAP.items():
                if api_id not in image_urls:
                    continue
                img = await self._client.download_image(image_urls[api_id])
                if img:
                    image_bytes[picture_key] = img
                    # Badge-Varianten: gleiche Basis-Bilder (ohne Overlay-Compositing)
                    image_bytes[f"{picture_key}WithBadge"] = img

            vehicles[vin] = {
                "vin": vin,
                "model": model,
                "nickname": nickname,
                "image_bytes": image_bytes,
            }

        _LOGGER.info("%d Fahrzeug(e) geladen", len(vehicles))
        return vehicles

    def async_cleanup(self) -> None:
        """Session aufräumen."""
        self._client = None
