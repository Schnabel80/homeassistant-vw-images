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

        except ConfigEntryAuthFailed:
            raise
        except WeConnectAuthError as err:
            _LOGGER.warning("Authentifizierungsfehler: %s", err)
            self._client = None
            raise ConfigEntryAuthFailed("Authentifizierung fehlgeschlagen") from err
        except WeConnectConnectionError as err:
            _LOGGER.warning("Verbindungsfehler: %s", err)
            raise UpdateFailed(f"Netzwerkfehler bei WeConnect-Verbindung: {err}") from err
        except Exception as err:
            _LOGGER.warning("WeConnect Update fehlgeschlagen: %s", err)
            self._client = None
            raise UpdateFailed(f"Fehler beim Abrufen der Fahrzeugdaten: {err}") from err

    def async_cleanup(self) -> None:
        """Session aufräumen."""
        self._client = None
