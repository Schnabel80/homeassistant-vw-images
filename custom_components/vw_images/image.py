"""Image-Entitäten für die VW Images Integration."""

import io
import logging
from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MAX_IMAGE_PIXELS
from .coordinator import VWImagesCoordinator

_LOGGER = logging.getLogger(__name__)

# Alle verfügbaren Bildtypen mit Entity-Name und Unique-ID-Suffix
PICTURE_TYPES = {
    "car": {
        "name": "Vehicle Image",
        "suffix": "image",
    },
    "carWithBadge": {
        "name": "Vehicle Image with Badges",
        "suffix": "image_badge",
    },
    "status": {
        "name": "Vehicle Status",
        "suffix": "status_image",
    },
    "statusWithBadge": {
        "name": "Vehicle Status with Badges",
        "suffix": "status_image_badge",
    },
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Image-Entitäten aus Config-Entry einrichten."""
    coordinator: VWImagesCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for vin, vehicle_data in coordinator.data.items():
        picture_refs = vehicle_data.get("picture_refs", {})
        for picture_key, config in PICTURE_TYPES.items():
            if picture_key in picture_refs:
                entities.append(
                    VehicleImageEntity(
                        coordinator,
                        vin,
                        vehicle_data,
                        picture_key=picture_key,
                        entity_name=config["name"],
                        unique_suffix=config["suffix"],
                    )
                )

    async_add_entities(entities)


class VehicleImageEntity(CoordinatorEntity[VWImagesCoordinator], ImageEntity):
    """Zeigt ein Fahrzeugbild eines VW-Fahrzeugs."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VWImagesCoordinator,
        vin: str,
        vehicle_data: dict,
        *,
        picture_key: str,
        entity_name: str,
        unique_suffix: str,
    ) -> None:
        """Initialisiere die Image-Entität."""
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._vin = vin
        self._picture_key = picture_key
        self._attr_unique_id = f"{DOMAIN}_{vin}_{unique_suffix}"
        self._attr_name = entity_name
        self._attr_image_last_updated = None
        self._cached_image_bytes: bytes | None = None

        # Device-Info: Gruppiert alle Entitäten eines Fahrzeugs
        display_name = vehicle_data.get("nickname") or vehicle_data.get("model", "VW")
        self._attr_device_info = {
            "identifiers": {(DOMAIN, vin)},
            "name": display_name,
            "manufacturer": "Volkswagen",
            "model": vehicle_data.get("model"),
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Reagiere auf Coordinator-Updates: Cache invalidieren, Zeitstempel setzen."""
        self._cached_image_bytes = None
        self._attr_image_last_updated = datetime.now()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        """Liefere das Fahrzeugbild als PNG-Bytes (mit Caching)."""
        if self._cached_image_bytes is not None:
            return self._cached_image_bytes

        try:
            if self.coordinator.data is None:
                return None

            vehicle_data = self.coordinator.data.get(self._vin)
            if vehicle_data is None:
                return None

            picture_refs = vehicle_data.get("picture_refs", {})
            pictures_ref = picture_refs.get(self._picture_key)
            if pictures_ref is None:
                return None

            # Bilder-Zugriff ist blocking → in Executor ausführen
            def _get_image_bytes():
                pil_image = pictures_ref.value
                if pil_image is None:
                    return None

                # Größenprüfung gegen Decompression-Bombs
                width, height = pil_image.size
                if width * height > MAX_IMAGE_PIXELS:
                    _LOGGER.warning(
                        "Bild zu groß (%dx%d), überspringe",
                        width, height,
                    )
                    return None

                buf = io.BytesIO()
                pil_image.save(buf, format="PNG")
                image_bytes = buf.getvalue()
                buf.close()
                return image_bytes

            self._cached_image_bytes = await self.hass.async_add_executor_job(
                _get_image_bytes
            )
            return self._cached_image_bytes

        except Exception:
            _LOGGER.debug(
                "Fehler beim Abrufen des Bilds (%s) für ***%s",
                self._picture_key,
                self._vin[-4:],
                exc_info=True,
            )
            return None

    @property
    def content_type(self) -> str:
        """Bild-Format: PNG."""
        return "image/png"
