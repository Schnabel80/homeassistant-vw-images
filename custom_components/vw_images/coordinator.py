"""Daten-Koordinator für die VW Images Integration."""

import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import MIN_REFRESH_INTERVAL

_LOGGER = logging.getLogger(__name__)


class VWImagesCoordinator(DataUpdateCoordinator):
    """Koordinator für VW Images.

    Kein automatisches Polling – Updates werden nur on-demand
    über async_request_refresh() ausgelöst (Button oder Service-Call).
    Rate-Limiting: Mindestens MIN_REFRESH_INTERVAL Sekunden zwischen Aufrufen.
    """

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialisiere den Koordinator ohne update_interval."""
        super().__init__(
            hass,
            _LOGGER,
            name="VW Images",
            # Kein update_interval → nur on-demand
        )
        self.config_entry = entry
        self._weconnect = None
        self._last_refresh_time: float = 0

    async def _async_setup(self) -> None:
        """Einmalige Einrichtung: WeConnect-Login."""
        from weconnect import weconnect as wc_module

        username = self.config_entry.data[CONF_USERNAME]
        password = self.config_entry.data[CONF_PASSWORD]

        self._weconnect = wc_module.WeConnect(
            username=username,
            password=password,
            updateAfterLogin=False,
            loginOnInit=False,
        )

        _LOGGER.info("WeConnect Login wird durchgeführt...")
        try:
            await self.hass.async_add_executor_job(self._weconnect.login)
        except Exception as err:
            self._weconnect = None
            raise ConfigEntryAuthFailed(
                "WeConnect-Anmeldung fehlgeschlagen. Bitte Zugangsdaten prüfen."
            ) from err
        _LOGGER.info("WeConnect Login erfolgreich")

    async def _async_update_data(self) -> dict:
        """Fahrzeugdaten von WeConnect abrufen (mit Rate-Limiting)."""
        # Rate-Limiting: Mindestabstand zwischen Aufrufen
        now = time.monotonic()
        elapsed = now - self._last_refresh_time
        if self._last_refresh_time > 0 and elapsed < MIN_REFRESH_INTERVAL:
            _LOGGER.debug(
                "Rate-Limit: Nächster Refresh in %d Sekunden möglich",
                int(MIN_REFRESH_INTERVAL - elapsed),
            )
            # Gib gecachte Daten zurück statt erneut abzufragen
            if self.data is not None:
                return self.data
            # Beim allerersten Mal trotzdem durchlassen

        try:
            if self._weconnect is None:
                await self._async_setup()

            _LOGGER.debug("Aktualisiere WeConnect Fahrzeugdaten...")
            await self.hass.async_add_executor_job(self._weconnect.update)
            self._last_refresh_time = time.monotonic()

            vehicles = {}
            for vin, vehicle in self._weconnect.vehicles.items():
                model = self._safe_attr(vehicle, "model")
                nickname = self._safe_attr(vehicle, "nickname")

                # Bild-Referenzen speichern, nicht das gesamte Vehicle-Objekt
                picture_refs = {}
                try:
                    if hasattr(vehicle, "pictures"):
                        for key in ("car", "carWithBadge", "status", "statusWithBadge"):
                            if key in vehicle.pictures:
                                picture_refs[key] = vehicle.pictures[key]
                except Exception:
                    _LOGGER.debug("Konnte Bild-Referenzen nicht lesen für ***%s", vin[-4:])

                vehicles[vin] = {
                    "vin": vin,
                    "model": model or "VW Fahrzeug",
                    "nickname": nickname,
                    "picture_refs": picture_refs,
                }

            _LOGGER.info("%d Fahrzeug(e) geladen", len(vehicles))
            return vehicles

        except ConfigEntryAuthFailed:
            raise
        except ConnectionError as err:
            _LOGGER.warning("Netzwerkfehler, Session wird zurückgesetzt")
            self._weconnect = None
            raise UpdateFailed("Netzwerkfehler bei WeConnect-Verbindung") from err
        except TimeoutError as err:
            _LOGGER.warning("Zeitüberschreitung, Session wird zurückgesetzt")
            self._weconnect = None
            raise UpdateFailed("Zeitüberschreitung bei WeConnect-Verbindung") from err
        except Exception as err:
            _LOGGER.warning("WeConnect Update fehlgeschlagen, Session wird zurückgesetzt")
            self._weconnect = None
            raise UpdateFailed("Fehler beim Abrufen der Fahrzeugdaten") from err

    def async_cleanup(self) -> None:
        """WeConnect-Session aufräumen."""
        if self._weconnect is not None:
            _LOGGER.debug("Beende WeConnect-Session")
            try:
                if hasattr(self._weconnect, "logout"):
                    self._weconnect.logout()
            except Exception:
                _LOGGER.debug("Fehler beim Logout", exc_info=True)
            self._weconnect = None

    @staticmethod
    def _safe_attr(obj, attr: str) -> str | None:
        """Sicherer Zugriff auf WeConnect-Attribut."""
        try:
            a = getattr(obj, attr, None)
            if a is not None and hasattr(a, "value") and a.value is not None:
                return str(a.value)
        except Exception:
            pass
        return None
