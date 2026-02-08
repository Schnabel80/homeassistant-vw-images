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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Image-Entitäten aus Config-Entry einrichten."""
    coordinator: VWImagesCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for vin, vehicle_data in coordinator.data.items():
        # Statisches 3/4-Ansicht-Foto
        entities.append(VehicleImageEntity(coordinator, vin, vehicle_data))
        # Dynamisches Statusbild (Türen, Fenster, Badges)
        if vehicle_data.get("pictures_status_ref") is not None:
            entities.append(VehicleStatusImageEntity(coordinator, vin, vehicle_data))

    async_add_entities(entities)


class VehicleImageEntity(CoordinatorEntity[VWImagesCoordinator], ImageEntity):
    """Zeigt das Fahrzeugbild eines VW-Fahrzeugs."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VWImagesCoordinator,
        vin: str,
        vehicle_data: dict,
    ) -> None:
        """Initialisiere die Image-Entität."""
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._vin = vin
        self._attr_unique_id = f"{DOMAIN}_{vin}_image"
        self._attr_name = "Fahrzeugbild"
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
        # Gecachtes Bild zurückgeben wenn vorhanden
        if self._cached_image_bytes is not None:
            return self._cached_image_bytes

        try:
            if self.coordinator.data is None:
                return None

            vehicle_data = self.coordinator.data.get(self._vin)
            if vehicle_data is None:
                return None

            pictures_ref = vehicle_data.get("pictures_car_ref")
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
                "Fehler beim Abrufen des Fahrzeugbilds für ***%s",
                self._vin[-4:],
                exc_info=True,
            )
            return None

    @property
    def content_type(self) -> str:
        """Bild-Format: PNG."""
        return "image/png"


class VehicleStatusImageEntity(CoordinatorEntity[VWImagesCoordinator], ImageEntity):
    """Zeigt das dynamische Statusbild eines VW-Fahrzeugs (Türen, Fenster, Badges)."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: VWImagesCoordinator,
        vin: str,
        vehicle_data: dict,
    ) -> None:
        """Initialisiere die Status-Image-Entität."""
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, coordinator.hass)
        self._vin = vin
        self._attr_unique_id = f"{DOMAIN}_{vin}_status_image"
        self._attr_name = "Vehicle Status"
        self._attr_image_last_updated = None
        self._cached_image_bytes: bytes | None = None

        # Device-Info: Gruppiert mit den anderen Entitäten des Fahrzeugs
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
        """Liefere das Statusbild als PNG-Bytes (mit Caching)."""
        if self._cached_image_bytes is not None:
            return self._cached_image_bytes

        try:
            if self.coordinator.data is None:
                return None

            vehicle_data = self.coordinator.data.get(self._vin)
            if vehicle_data is None:
                return None

            pictures_ref = vehicle_data.get("pictures_status_ref")
            if pictures_ref is None:
                return None

            def _get_image_bytes():
                pil_image = pictures_ref.value
                if pil_image is None:
                    return None

                width, height = pil_image.size
                if width * height > MAX_IMAGE_PIXELS:
                    _LOGGER.warning(
                        "Statusbild zu groß (%dx%d), überspringe",
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
                "Fehler beim Abrufen des Statusbilds für ***%s",
                self._vin[-4:],
                exc_info=True,
            )
            return None

    @property
    def content_type(self) -> str:
        """Bild-Format: PNG."""
        return "image/png"
