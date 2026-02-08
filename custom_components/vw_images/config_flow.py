"""Config-Flow für die VW Images Integration."""

import logging

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class VWImagesConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Config-Flow: Zugangsdaten für VW WeConnect eingeben."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Erster Schritt: Username und Passwort abfragen."""
        errors = {}

        if user_input is not None:
            # Zugangsdaten validieren
            result, error_key = await self._validate_credentials(
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )

            if result:
                # Prüfe ob bereits ein Eintrag mit diesem Username existiert
                await self.async_set_unique_id(user_input[CONF_USERNAME])
                self._abort_if_unique_id_configured()

                # Generischer Titel statt E-Mail-Adresse (Fix #3: Keine PII im Titel)
                return self.async_create_entry(
                    title="VW Images",
                    data=user_input,
                )

            errors["base"] = error_key

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data=None):
        """Reauth: Wird aufgerufen wenn ConfigEntryAuthFailed geworfen wird."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Reauth-Formular: Neue Zugangsdaten eingeben."""
        errors = {}

        if user_input is not None:
            result, error_key = await self._validate_credentials(
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )

            if result:
                # Bestehenden Config-Entry mit neuen Daten aktualisieren
                entry = self.hass.config_entries.async_get_entry(
                    self.context["entry_id"]
                )
                self.hass.config_entries.async_update_entry(
                    entry, data=user_input
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

            errors["base"] = error_key

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    async def _validate_credentials(self, username: str, password: str) -> tuple[bool, str | None]:
        """Validiere WeConnect-Zugangsdaten. Gibt (success, error_key) zurück."""
        try:
            from weconnect import weconnect as wc_module

            wc = wc_module.WeConnect(
                username=username,
                password=password,
                updateAfterLogin=False,
                loginOnInit=False,
            )
            await self.hass.async_add_executor_job(wc.login)
            return True, None

        except ConnectionError:
            _LOGGER.warning("Netzwerkfehler bei WeConnect-Verbindung")
            return False, "cannot_connect"
        except TimeoutError:
            _LOGGER.warning("Zeitüberschreitung bei WeConnect-Verbindung")
            return False, "cannot_connect"
        except Exception:
            _LOGGER.debug("Authentifizierung fehlgeschlagen", exc_info=True)
            return False, "invalid_auth"
