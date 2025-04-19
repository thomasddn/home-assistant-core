"""Config flow for Volvo."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
import logging
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from volvocarsapi.api import VolvoCarsApi
from volvocarsapi.models import VolvoApiException

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
)
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_API_KEY, CONF_NAME, CONF_TOKEN
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowHandler, FlowResult
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.config_entry_oauth2_flow import AbstractOAuth2FlowHandler
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from .api import ConfigFlowVolvoAuth
from .const import (
    CONF_VIN,
    DOMAIN,
    MANUFACTURER,
    OPT_FUEL_CONSUMPTION_UNIT,
    OPT_FUEL_UNIT_LITER_PER_100KM,
    OPT_FUEL_UNIT_MPG_UK,
    OPT_FUEL_UNIT_MPG_US,
)
from .coordinator import VolvoConfigEntry, VolvoData

_LOGGER = logging.getLogger(__name__)


def _default_fuel_unit(hass: HomeAssistant) -> str:
    if hass.config.country == "UK":
        return OPT_FUEL_UNIT_MPG_UK

    if hass.config.units == US_CUSTOMARY_SYSTEM or hass.config.country == "US":
        return OPT_FUEL_UNIT_MPG_US

    return OPT_FUEL_UNIT_LITER_PER_100KM


class VolvoConfigBase(ABC):
    """Base class for Volvo config flows."""

    @abstractmethod
    async def async_create_or_update(self) -> FlowResult:
        """Create or update the entry."""
        raise NotImplementedError

    @abstractmethod
    def get_token(self) -> str:
        """Get the access token."""
        raise NotImplementedError

    @abstractmethod
    def get_user_input(self) -> dict[str, Any] | None:
        """Get user input."""
        raise NotImplementedError


class VolvoConfigFlow(VolvoConfigBase, FlowHandler):
    """Base class for Volvo flow handlers."""


class VolvoOAuth2FlowHandler(AbstractOAuth2FlowHandler, VolvoConfigFlow, domain=DOMAIN):
    """Config flow to handle Volvo OAuth2 authentication."""

    DOMAIN = DOMAIN

    def __init__(self) -> None:
        """Initialize Volvo config flow."""
        super().__init__()

        self._helper = VolvoConfigHelper(self)

    @property
    def logger(self) -> logging.Logger:
        """Return logger."""
        return _LOGGER

    # Overridden method
    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return subentries supported by this handler."""
        return {"vehicle": VolvoSubentryFlowHandler}

    # Overridden method
    async def async_oauth_create_entry(self, data: dict) -> ConfigFlowResult:
        """Create an entry for the flow."""
        self._helper.config_data |= data
        return await self._helper.async_step_api_key()

    # By convention method
    async def async_step_reauth(self, _: Mapping[str, Any]) -> ConfigFlowResult:
        """Perform reauth upon an API authentication error."""
        return await self.async_step_reauth_confirm()

    # By convention method
    async def async_step_reconfigure(
        self, _: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure the entry."""
        return await self._helper.async_step_api_key()

    # Overridden method
    @staticmethod
    @callback
    def async_get_options_flow(_: VolvoConfigEntry) -> VolvoOptionsFlowHandler:
        """Create the options flow."""
        return VolvoOptionsFlowHandler()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reauth dialog."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                description_placeholders={CONF_NAME: self._get_reauth_entry().title},
            )
        return await self.async_step_user()

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the API key step."""
        return await self._helper.async_step_api_key(user_input)

    async def async_step_vin(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the VIN step."""
        return await self._helper.async_step_vin(user_input)

    # Overridden method of VolvoConfigBase
    async def async_create_or_update(self) -> ConfigFlowResult:
        """Create or update the entry."""
        vin = self._helper.config_data[CONF_VIN]
        await self.async_set_unique_id(vin)

        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(),
                data_updates=self._helper.config_data,
            )

        if self.source == SOURCE_RECONFIGURE:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(),
                data_updates=self._helper.config_data,
                reload_even_if_entry_is_unchanged=False,
            )

        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"{MANUFACTURER} {vin}",
            data=self._helper.config_data,
            options={OPT_FUEL_CONSUMPTION_UNIT: _default_fuel_unit(self.hass)},
        )

    # Overridden method of VolvoConfigBase
    def get_token(self) -> str:
        """Get the access token."""
        return self._helper.config_data[CONF_TOKEN][CONF_ACCESS_TOKEN]

    # Overridden method of VolvoConfigBase
    def get_user_input(self) -> dict[str, Any] | None:
        """Get user input."""
        user_input = None

        if self.source == SOURCE_REAUTH:
            user_input = self._helper.config_data = dict(self._get_reauth_entry().data)
        elif self.source == SOURCE_RECONFIGURE:
            user_input = self._helper.config_data = dict(
                self._get_reconfigure_entry().data
            )

        return user_input


class VolvoSubentryFlowHandler(ConfigSubentryFlow, VolvoConfigFlow):
    """Handle subentry flow."""

    def __init__(self) -> None:
        """Initialize subentry flow hanlder."""
        super().__init__()

        self._helper = VolvoConfigHelper(self)

    # Overridden method
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """User flow to create a Volvo subentry."""
        return await self._helper.async_step_api_key()

    # By convention method
    async def async_step_reconfigure(
        self, _: dict[str, Any] | None = None
    ) -> FlowResult:
        """Reconfigure the entry."""
        return await self.async_step_user()

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the API key step."""
        return await self._helper.async_step_api_key(user_input)

    async def async_step_vin(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the VIN step."""
        return await self._helper.async_step_vin(user_input)

    # Overridden method of VolvoConfigBase
    async def async_create_or_update(self) -> ConfigFlowResult:
        """Create or update the entry."""
        vin = self._helper.config_data[CONF_VIN]

        return self.async_create_entry(
            title=f"{MANUFACTURER} {vin}",
            data=self._helper.config_data,
            unique_id=vin,
        )

    # Overridden method of VolvoConfigBase
    def get_token(self) -> str:
        """Get the access token."""
        entry = self._get_entry()
        return str(entry.data[CONF_TOKEN][CONF_ACCESS_TOKEN])

    # Overridden method of VolvoConfigBase
    def get_user_input(self) -> dict[str, Any] | None:
        """Get user input."""
        user_input = None

        if self.source == SOURCE_RECONFIGURE:
            user_input = self._helper.config_data = dict(
                self._get_reconfigure_subentry().data
            )

        return user_input


class VolvoOptionsFlowHandler(OptionsFlow):
    """Class to handle the options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        if TYPE_CHECKING:
            assert isinstance(self.config_entry.runtime_data, VolvoData)

        coordinator = self.config_entry.runtime_data.coordinator
        schema: dict[vol.Marker, Any] = {}

        if coordinator.vehicle.has_combustion_engine():
            schema.update(
                {
                    vol.Required(
                        OPT_FUEL_CONSUMPTION_UNIT,
                        default=self.config_entry.options.get(
                            OPT_FUEL_CONSUMPTION_UNIT, OPT_FUEL_UNIT_LITER_PER_100KM
                        ),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                OPT_FUEL_UNIT_LITER_PER_100KM,
                                OPT_FUEL_UNIT_MPG_UK,
                                OPT_FUEL_UNIT_MPG_US,
                            ],
                            multiple=False,
                            translation_key=OPT_FUEL_CONSUMPTION_UNIT,
                        )
                    )
                }
            )

        if len(schema) == 0:
            return self.async_abort(reason="no_options_available")

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(schema), self.config_entry.options
            ),
        )


class VolvoConfigHelper:
    """Helper class for Volvo config flows."""

    def __init__(self, flow_handler: VolvoConfigFlow) -> None:
        """Initialize config flow helper."""
        self.flow_handler = flow_handler

        self.vins: list[str] = []
        self.config_data: dict[str, Any] = {}

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the API key step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            web_session = aiohttp_client.async_get_clientsession(self.flow_handler.hass)
            token = self.flow_handler.get_token()
            auth = ConfigFlowVolvoAuth(web_session, token)
            api = VolvoCarsApi(web_session, auth, "", user_input[CONF_API_KEY])

            try:
                self.vins = await api.async_get_vehicles()
            except VolvoApiException:
                _LOGGER.exception("Unable to retrieve vehicles")
                errors["base"] = "cannot_load_vehicles"

            if not errors:
                self.config_data |= user_input

                if len(self.vins) == 1:
                    # If there is only one VIN, take that as value and
                    # immediately create the entry. No need to show
                    # additional step.
                    self.config_data[CONF_VIN] = self.vins[0]
                    return await self.flow_handler.async_create_or_update()

                if self.flow_handler.source in (SOURCE_REAUTH, SOURCE_RECONFIGURE):
                    # Don't let users change the VIN. The entry should be
                    # recreated if they want to change the VIN.
                    return await self.flow_handler.async_create_or_update()

                return await self.async_step_vin()

        if user_input is None:
            user_input = self.flow_handler.get_user_input() or {}

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_API_KEY, default=user_input.get(CONF_API_KEY, "")
                ): str,
            },
        )

        return self.flow_handler.async_show_form(
            step_id="api_key", data_schema=schema, errors=errors
        )

    async def async_step_vin(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the VIN step."""
        if user_input is not None:
            self.config_data |= user_input
            return await self.flow_handler.async_create_or_update()

        schema = vol.Schema(
            {
                vol.Required(CONF_VIN): SelectSelector(
                    SelectSelectorConfig(
                        options=self.vins,
                        multiple=False,
                    )
                ),
            },
        )

        return self.flow_handler.async_show_form(step_id="vin", data_schema=schema)
