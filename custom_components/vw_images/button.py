"""Button-Entitäten für die VW Images Integration."""

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import VWImagesCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Button-Entitäten aus Config-Entry einrichten."""
    coordinator: VWImagesCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        UpdateImageButton(coordinator, vin, vehicle_data)
        for vin, vehicle_data in coordinator.data.items()
    ]

    async_add_entities(entities)


class UpdateImageButton(CoordinatorEntity[VWImagesCoordinator], ButtonEntity):
    """Button zum manuellen Aktualisieren des Fahrzeugbilds."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VWImagesCoordinator,
        vin: str,
        vehicle_data: dict,
    ) -> None:
        """Initialisiere den Button."""
        super().__init__(coordinator)
        self._vin = vin
        self._attr_unique_id = f"{DOMAIN}_{vin}_update_button"
        self._attr_name = "Bild aktualisieren"
        self._attr_icon = "mdi:refresh"

        # Device-Info: Gruppiert mit der Image-Entität
        display_name = vehicle_data.get("nickname") or vehicle_data.get("model", "VW")
        self._attr_device_info = {
            "identifiers": {(DOMAIN, vin)},
            "name": display_name,
            "manufacturer": "Volkswagen",
            "model": vehicle_data.get("model"),
        }

    @property
    def available(self) -> bool:
        """Button ist immer verfügbar, auch bei fehlgeschlagenem Update."""
        return True

    async def async_press(self) -> None:
        """Button gedrückt: Fahrzeugbilder aktualisieren."""
        _LOGGER.debug("Bild-Update angefordert für ***%s", self._vin[-4:])
        await self.coordinator.async_request_refresh()
