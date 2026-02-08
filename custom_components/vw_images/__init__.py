"""VW Images – Home Assistant Integration.

Stellt für jedes VW-Fahrzeug im WeConnect-Account ein Fahrzeugbild
als Image-Entität bereit. Bilder werden nur on-demand aktualisiert
(Button oder Service-Call).
"""

import logging
import re

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall

from .const import DOMAIN, SERVICE_UPDATE_IMAGES
from .coordinator import VWImagesCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.IMAGE, Platform.BUTTON]

# VIN: 17 Zeichen, alphanumerisch ohne I, O, Q
VIN_REGEX = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")

SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("vin"): vol.All(str, vol.Match(r"^[A-HJ-NPR-Z0-9]{17}$")),
    }
)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Globales Setup: Service registrieren."""

    async def handle_update_images(call: ServiceCall) -> None:
        """Service-Handler: Fahrzeugbilder aktualisieren."""
        target_vin = call.data.get("vin")

        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            _LOGGER.warning("Kein VW Images Eintrag konfiguriert")
            return

        for entry in entries:
            if entry.state.name != "LOADED":
                continue

            coordinator: VWImagesCoordinator = hass.data[DOMAIN].get(entry.entry_id)
            if coordinator is None:
                continue

            if target_vin:
                if coordinator.data and target_vin in coordinator.data:
                    _LOGGER.debug("Aktualisiere Bild für VIN ***%s", target_vin[-4:])
                    await coordinator.async_request_refresh()
            else:
                _LOGGER.debug("Aktualisiere alle Fahrzeugbilder")
                await coordinator.async_request_refresh()

    hass.services.async_register(
        DOMAIN,
        SERVICE_UPDATE_IMAGES,
        handle_update_images,
        schema=SERVICE_SCHEMA,
    )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config-Entry einrichten: Coordinator starten, Plattformen laden."""
    hass.data.setdefault(DOMAIN, {})

    coordinator = VWImagesCoordinator(hass, entry)

    # Erster Datenabruf (inkl. Login)
    await coordinator.async_config_entry_first_refresh()

    # Coordinator speichern
    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Plattformen laden (image, button)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Config-Entry entladen und WeConnect-Session beenden."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        coordinator: VWImagesCoordinator | None = hass.data[DOMAIN].pop(
            entry.entry_id, None
        )
        if coordinator is not None:
            coordinator.async_cleanup()

    return unload_ok
