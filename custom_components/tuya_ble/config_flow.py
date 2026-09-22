"""Config flow for Tuya BLE integration."""

from __future__ import annotations

import logging
import pycountry
from typing import Any

import voluptuous as vol
from tuya_iot import AuthType

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.const import (
    CONF_ADDRESS,
    CONF_COUNTRY_CODE,
    CONF_PASSWORD,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowHandler, FlowResult
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .tuya_ble import SERVICE_UUIDS, TuyaBLEDeviceCredentials

from .const import (
    CONF_BLE_CONTROL_ENABLED,
    CONF_CONNECTION_MODE,
    CONF_ON_DEMAND_CONNECTION_HOLD_TIME,
    ConnectionMode,
    DEFAULT_BLE_CONTROL_ENABLED,
    DEFAULT_CONNECTION_MODE,
    DEFAULT_ON_DEMAND_CONNECTION_HOLD_TIME,
    TUYA_COUNTRIES,
    TUYA_SMART_APP,
    SMARTLIFE_APP,
    TUYA_RESPONSE_SUCCESS,
    TUYA_RESPONSE_CODE,
    TUYA_RESPONSE_MSG,
    CONF_ACCESS_ID,
    CONF_ACCESS_SECRET,
    CONF_APP_TYPE,
    CONF_AUTH_TYPE,
    CONF_ENDPOINT,
    CONF_SEC_KEY,
    DOMAIN,
    normalize_on_demand_connection_hold_time,
    validate_on_demand_connection_hold_time,
)
from .devices import TuyaBLEData, get_device_readable_name
from .cloud import HASSTuyaBLEDeviceManager

_LOGGER = logging.getLogger(__name__)


async def _try_login(
    manager: HASSTuyaBLEDeviceManager,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, Any],
) -> dict[str, Any] | None:
    response: dict[Any, Any] | None
    data: dict[str, Any]

    country = [
        country
        for country in TUYA_COUNTRIES
        if country.name == user_input[CONF_COUNTRY_CODE]
    ][0]

    data = {
        CONF_ENDPOINT: country.endpoint,
        CONF_AUTH_TYPE: AuthType.CUSTOM,
        CONF_ACCESS_ID: user_input[CONF_ACCESS_ID],
        CONF_ACCESS_SECRET: user_input[CONF_ACCESS_SECRET],
        CONF_USERNAME: user_input[CONF_USERNAME],
        CONF_PASSWORD: user_input[CONF_PASSWORD],
        CONF_COUNTRY_CODE: country.country_code,
    }
    if sec_key := user_input.get(CONF_SEC_KEY):
        data[CONF_SEC_KEY] = sec_key

    for app_type in (TUYA_SMART_APP, SMARTLIFE_APP, ""):
        data[CONF_APP_TYPE] = app_type
        if app_type == "":
            data[CONF_AUTH_TYPE] = AuthType.CUSTOM
        else:
            data[CONF_AUTH_TYPE] = AuthType.SMART_HOME

        response = await manager._login(data, True)

        if response.get(TUYA_RESPONSE_SUCCESS, False):
            return data

    errors["base"] = "login_error"
    if response:
        placeholders.update(
            {
                TUYA_RESPONSE_CODE: response.get(TUYA_RESPONSE_CODE),
                TUYA_RESPONSE_MSG: response.get(TUYA_RESPONSE_MSG),
            }
        )

    return None


def _show_login_form(
    flow: FlowHandler,
    user_input: dict[str, Any],
    errors: dict[str, str],
    placeholders: dict[str, Any],
) -> FlowResult:
    """Shows the Tuya IOT platform login form."""
    if user_input is not None and user_input.get(CONF_COUNTRY_CODE) is not None:
        for country in TUYA_COUNTRIES:
            if country.country_code == user_input[CONF_COUNTRY_CODE]:
                user_input[CONF_COUNTRY_CODE] = country.name
                break

    def_country_name: str | None = None
    try:
        def_country = pycountry.countries.get(alpha_2=flow.hass.config.country)
        if def_country:
            def_country_name = def_country.name
    except:
        pass

    placeholders["url"] = "https://www.home-assistant.io/integrations/tuya/"

    return flow.async_show_form(
        step_id="login",
        data_schema=vol.Schema(
            {
                vol.Required(
                    CONF_COUNTRY_CODE,
                    default=user_input.get(CONF_COUNTRY_CODE, def_country_name),
                ): vol.In(
                    # We don't pass a dict {code:name} because country codes can be duplicate.
                    [country.name for country in TUYA_COUNTRIES]
                ),
                vol.Required(
                    CONF_ACCESS_ID, default=user_input.get(CONF_ACCESS_ID, "")
                ): str,
                vol.Required(
                    CONF_ACCESS_SECRET,
                    default=user_input.get(CONF_ACCESS_SECRET, ""),
                ): str,
                vol.Optional(
                    CONF_SEC_KEY,
                    default=user_input.get(CONF_SEC_KEY, ""),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                vol.Required(
                    CONF_USERNAME, default=user_input.get(CONF_USERNAME, "")
                ): str,
                vol.Required(
                    CONF_PASSWORD, default=user_input.get(CONF_PASSWORD, "")
                ): str,
            }
        ),
        errors=errors,
        description_placeholders=placeholders,
    )


class TuyaBLEOptionsFlow(OptionsFlowWithConfigEntry):
    """Handle a Tuya BLE options flow."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Initialize options flow."""
        super().__init__(config_entry)
        self._flow_config_entry = config_entry

    @property
    def _entry(self) -> ConfigEntry:
        """Return the linked entry across Home Assistant Options Flow APIs."""
        try:
            return self.config_entry
        except (AttributeError, ValueError):
            return self._flow_config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        return self.async_show_menu(
            step_id="init",
            menu_options=["connection_settings", "login"],
        )

    async def async_step_connection_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage connection mode and Home Assistant BLE control."""
        errors: dict[str, str] = {}
        domain_data = self.hass.data.get(DOMAIN, {})
        entry_data: TuyaBLEData | None = domain_data.get(self._entry.entry_id)
        supports_hold_time = bool(
            entry_data and entry_data.device.supports_on_demand_connection_hold_time
        )
        if user_input is not None:
            hold_time_supplied = CONF_ON_DEMAND_CONNECTION_HOLD_TIME in user_input
            raw_mode = user_input.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
            raw_enabled = user_input.get(
                CONF_BLE_CONTROL_ENABLED, DEFAULT_BLE_CONTROL_ENABLED
            )
            raw_hold_time = user_input.get(
                CONF_ON_DEMAND_CONNECTION_HOLD_TIME,
                (
                    entry_data.device.on_demand_connection_hold_time
                    if supports_hold_time and entry_data
                    else DEFAULT_ON_DEMAND_CONNECTION_HOLD_TIME
                ),
            )
            try:
                mode = ConnectionMode(raw_mode)
                if not isinstance(raw_enabled, bool):
                    raise ValueError
                hold_time = (
                    validate_on_demand_connection_hold_time(raw_hold_time)
                    if supports_hold_time and hold_time_supplied
                    else None
                )
            except (OverflowError, TypeError, ValueError):
                errors["base"] = "ble_policy_transition_failed"
            else:
                try:
                    if entry_data:
                        policy_updates: dict[str, Any] = {
                            "connection_mode": mode.value,
                            "ble_control_enabled": raw_enabled,
                        }
                        if hold_time is not None:
                            policy_updates[CONF_ON_DEMAND_CONNECTION_HOLD_TIME] = (
                                hold_time
                            )
                        await entry_data.device.async_update_connection_policy(
                            **policy_updates
                        )
                except Exception:  # noqa: BLE001
                    errors["base"] = "ble_policy_transition_failed"
                else:
                    options = dict(self._entry.options)
                    options.update(
                        {
                            CONF_CONNECTION_MODE: mode.value,
                            CONF_BLE_CONTROL_ENABLED: raw_enabled,
                        }
                    )
                    if hold_time is not None:
                        options[CONF_ON_DEMAND_CONNECTION_HOLD_TIME] = hold_time
                    return self.async_create_entry(
                        title=self._entry.title,
                        data=options,
                    )

        options = self._entry.options
        try:
            default_mode = ConnectionMode(
                options.get(CONF_CONNECTION_MODE, DEFAULT_CONNECTION_MODE)
            ).value
        except (TypeError, ValueError):
            default_mode = DEFAULT_CONNECTION_MODE
        default_enabled = options.get(
            CONF_BLE_CONTROL_ENABLED, DEFAULT_BLE_CONTROL_ENABLED
        )
        if not isinstance(default_enabled, bool):
            default_enabled = DEFAULT_BLE_CONTROL_ENABLED
        default_hold_time = normalize_on_demand_connection_hold_time(
            options.get(
                CONF_ON_DEMAND_CONNECTION_HOLD_TIME,
                DEFAULT_ON_DEMAND_CONNECTION_HOLD_TIME,
            )
        )
        schema: dict[vol.Marker, object] = {
            vol.Required(
                CONF_CONNECTION_MODE,
                default=default_mode,
            ): vol.In([mode.value for mode in ConnectionMode]),
            vol.Required(
                CONF_BLE_CONTROL_ENABLED,
                default=default_enabled,
            ): bool,
        }
        if supports_hold_time:
            schema[
                vol.Required(
                    CONF_ON_DEMAND_CONNECTION_HOLD_TIME,
                    default=default_hold_time,
                )
            ] = vol.All(
                int,
                vol.Range(
                    min
