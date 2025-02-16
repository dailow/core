"""Support for Xiaomi Yeelight WiFi color bulb."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging

import voluptuous as vol
from yeelight import BulbException, discover_bulbs
from yeelight.aio import KEY_CONNECTED, AsyncBulb

from homeassistant.config_entries import SOURCE_IMPORT, ConfigEntry, ConfigEntryNotReady
from homeassistant.const import (
    CONF_DEVICES,
    CONF_HOST,
    CONF_ID,
    CONF_NAME,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import (
    async_dispatcher_connect,
    async_dispatcher_send,
)
from homeassistant.helpers.entity import DeviceInfo, Entity

_LOGGER = logging.getLogger(__name__)

DOMAIN = "yeelight"
DATA_YEELIGHT = DOMAIN
DATA_UPDATED = "yeelight_{}_data_updated"
DEVICE_INITIALIZED = "yeelight_{}_device_initialized"

DEFAULT_NAME = "Yeelight"
DEFAULT_TRANSITION = 350
DEFAULT_MODE_MUSIC = False
DEFAULT_SAVE_ON_CHANGE = False
DEFAULT_NIGHTLIGHT_SWITCH = False

CONF_MODEL = "model"
CONF_TRANSITION = "transition"
CONF_SAVE_ON_CHANGE = "save_on_change"
CONF_MODE_MUSIC = "use_music_mode"
CONF_FLOW_PARAMS = "flow_params"
CONF_CUSTOM_EFFECTS = "custom_effects"
CONF_NIGHTLIGHT_SWITCH_TYPE = "nightlight_switch_type"
CONF_NIGHTLIGHT_SWITCH = "nightlight_switch"

DATA_CONFIG_ENTRIES = "config_entries"
DATA_CUSTOM_EFFECTS = "custom_effects"
DATA_DEVICE = "device"
DATA_REMOVE_INIT_DISPATCHER = "remove_init_dispatcher"
DATA_PLATFORMS_LOADED = "platforms_loaded"

ATTR_COUNT = "count"
ATTR_ACTION = "action"
ATTR_TRANSITIONS = "transitions"
ATTR_MODE_MUSIC = "music_mode"

ACTION_RECOVER = "recover"
ACTION_STAY = "stay"
ACTION_OFF = "off"

ACTIVE_MODE_NIGHTLIGHT = "1"
ACTIVE_COLOR_FLOWING = "1"

NIGHTLIGHT_SWITCH_TYPE_LIGHT = "light"

DISCOVERY_INTERVAL = timedelta(seconds=60)

YEELIGHT_RGB_TRANSITION = "RGBTransition"
YEELIGHT_HSV_TRANSACTION = "HSVTransition"
YEELIGHT_TEMPERATURE_TRANSACTION = "TemperatureTransition"
YEELIGHT_SLEEP_TRANSACTION = "SleepTransition"

YEELIGHT_FLOW_TRANSITION_SCHEMA = {
    vol.Optional(ATTR_COUNT, default=0): cv.positive_int,
    vol.Optional(ATTR_ACTION, default=ACTION_RECOVER): vol.Any(
        ACTION_RECOVER, ACTION_OFF, ACTION_STAY
    ),
    vol.Required(ATTR_TRANSITIONS): [
        {
            vol.Exclusive(YEELIGHT_RGB_TRANSITION, CONF_TRANSITION): vol.All(
                cv.ensure_list, [cv.positive_int]
            ),
            vol.Exclusive(YEELIGHT_HSV_TRANSACTION, CONF_TRANSITION): vol.All(
                cv.ensure_list, [cv.positive_int]
            ),
            vol.Exclusive(YEELIGHT_TEMPERATURE_TRANSACTION, CONF_TRANSITION): vol.All(
                cv.ensure_list, [cv.positive_int]
            ),
            vol.Exclusive(YEELIGHT_SLEEP_TRANSACTION, CONF_TRANSITION): vol.All(
                cv.ensure_list, [cv.positive_int]
            ),
        }
    ],
}

DEVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_TRANSITION, default=DEFAULT_TRANSITION): cv.positive_int,
        vol.Optional(CONF_MODE_MUSIC, default=False): cv.boolean,
        vol.Optional(CONF_SAVE_ON_CHANGE, default=False): cv.boolean,
        vol.Optional(CONF_NIGHTLIGHT_SWITCH_TYPE): vol.Any(
            NIGHTLIGHT_SWITCH_TYPE_LIGHT
        ),
        vol.Optional(CONF_MODEL): cv.string,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_DEVICES, default={}): {cv.string: DEVICE_SCHEMA},
                vol.Optional(CONF_CUSTOM_EFFECTS): [
                    {
                        vol.Required(CONF_NAME): cv.string,
                        vol.Required(CONF_FLOW_PARAMS): YEELIGHT_FLOW_TRANSITION_SCHEMA,
                    }
                ],
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)

UPDATE_REQUEST_PROPERTIES = [
    "power",
    "main_power",
    "bright",
    "ct",
    "rgb",
    "hue",
    "sat",
    "color_mode",
    "flowing",
    "bg_power",
    "bg_lmode",
    "bg_flowing",
    "bg_ct",
    "bg_bright",
    "bg_hue",
    "bg_sat",
    "bg_rgb",
    "nl_br",
    "active_mode",
]

PLATFORMS = ["binary_sensor", "light"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Yeelight bulbs."""
    conf = config.get(DOMAIN, {})
    hass.data[DOMAIN] = {
        DATA_CUSTOM_EFFECTS: conf.get(CONF_CUSTOM_EFFECTS, {}),
        DATA_CONFIG_ENTRIES: {},
    }

    # Import manually configured devices
    for host, device_config in config.get(DOMAIN, {}).get(CONF_DEVICES, {}).items():
        _LOGGER.debug("Importing configured %s", host)
        entry_config = {CONF_HOST: host, **device_config}
        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=entry_config
            )
        )

    return True


async def _async_initialize(
    hass: HomeAssistant,
    entry: ConfigEntry,
    host: str,
    device: YeelightDevice | None = None,
) -> None:
    entry_data = hass.data[DOMAIN][DATA_CONFIG_ENTRIES][entry.entry_id] = {
        DATA_PLATFORMS_LOADED: False
    }
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    @callback
    def _async_load_platforms():
        if entry_data[DATA_PLATFORMS_LOADED]:
            return
        entry_data[DATA_PLATFORMS_LOADED] = True
        hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    if not device:
        device = await _async_get_device(hass, host, entry)
    entry_data[DATA_DEVICE] = device

    # start listening for local pushes
    await device.bulb.async_listen(device.async_update_callback)

    # register stop callback to shutdown listening for local pushes
    async def async_stop_listen_task(event):
        """Stop listen thread."""
        _LOGGER.debug("Shutting down Yeelight Listener")
        await device.bulb.async_stop_listening()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_stop_listen_task)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, DEVICE_INITIALIZED.format(host), _async_load_platforms
        )
    )

    # fetch initial state
    asyncio.create_task(device.async_update())


@callback
def _async_populate_entry_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move options from data for imported entries.

    Initialize options with default values for other entries.
    """
    if entry.options:
        return

    hass.config_entries.async_update_entry(
        entry,
        data={CONF_HOST: entry.data.get(CONF_HOST), CONF_ID: entry.data.get(CONF_ID)},
        options={
            CONF_NAME: entry.data.get(CONF_NAME, ""),
            CONF_MODEL: entry.data.get(CONF_MODEL, ""),
            CONF_TRANSITION: entry.data.get(CONF_TRANSITION, DEFAULT_TRANSITION),
            CONF_MODE_MUSIC: entry.data.get(CONF_MODE_MUSIC, DEFAULT_MODE_MUSIC),
            CONF_SAVE_ON_CHANGE: entry.data.get(
                CONF_SAVE_ON_CHANGE, DEFAULT_SAVE_ON_CHANGE
            ),
            CONF_NIGHTLIGHT_SWITCH: entry.data.get(
                CONF_NIGHTLIGHT_SWITCH, DEFAULT_NIGHTLIGHT_SWITCH
            ),
        },
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Yeelight from a config entry."""
    _async_populate_entry_options(hass, entry)

    if entry.data.get(CONF_HOST):
        try:
            device = await _async_get_device(hass, entry.data[CONF_HOST], entry)
        except OSError as ex:
            # If CONF_ID is not valid we cannot fallback to discovery
            # so we must retry by raising ConfigEntryNotReady
            if not entry.data.get(CONF_ID):
                raise ConfigEntryNotReady from ex
            # Otherwise fall through to discovery
        else:
            # manually added device
            try:
                await _async_initialize(
                    hass, entry, entry.data[CONF_HOST], device=device
                )
            except BulbException as ex:
                raise ConfigEntryNotReady from ex
            return True

    # discovery
    scanner = YeelightScanner.async_get(hass)

    async def _async_from_discovery(host: str) -> None:
        try:
            await _async_initialize(hass, entry, host)
        except BulbException:
            _LOGGER.exception("Failed to connect to bulb at %s", host)

    scanner.async_register_callback(entry.data[CONF_ID], _async_from_discovery)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data_config_entries = hass.data[DOMAIN][DATA_CONFIG_ENTRIES]
    entry_data = data_config_entries[entry.entry_id]

    if entry_data[DATA_PLATFORMS_LOADED]:
        if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
            return False

    if entry.data.get(CONF_ID):
        # discovery
        scanner = YeelightScanner.async_get(hass)
        scanner.async_unregister_callback(entry.data[CONF_ID])

    device = entry_data[DATA_DEVICE]
    _LOGGER.debug("Shutting down Yeelight Listener")
    await device.bulb.async_stop_listening()
    _LOGGER.debug("Yeelight Listener stopped")

    data_config_entries.pop(entry.entry_id)

    return True


@callback
def _async_unique_name(capabilities: dict) -> str:
    """Generate name from capabilities."""
    model = capabilities["model"]
    unique_id = capabilities["id"]
    return f"yeelight_{model}_{unique_id}"


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry):
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


class YeelightScanner:
    """Scan for Yeelight devices."""

    _scanner = None

    @classmethod
    @callback
    def async_get(cls, hass: HomeAssistant):
        """Get scanner instance."""
        if cls._scanner is None:
            cls._scanner = cls(hass)
        return cls._scanner

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize class."""
        self._hass = hass
        self._seen = {}
        self._callbacks = {}
        self._scan_task = None

    async def _async_scan(self):
        _LOGGER.debug("Yeelight scanning")
        # Run 3 times as packets can get lost
        for _ in range(3):
            devices = await self._hass.async_add_executor_job(discover_bulbs)
            for device in devices:
                unique_id = device["capabilities"]["id"]
                if unique_id in self._seen:
                    continue
                host = device["ip"]
                self._seen[unique_id] = host
                _LOGGER.debug("Yeelight discovered at %s", host)
                if unique_id in self._callbacks:
                    self._hass.async_create_task(self._callbacks[unique_id](host))
                    self._callbacks.pop(unique_id)
                    if len(self._callbacks) == 0:
                        self._async_stop_scan()

        await asyncio.sleep(DISCOVERY_INTERVAL.total_seconds())
        self._scan_task = self._hass.loop.create_task(self._async_scan())

    @callback
    def _async_start_scan(self):
        """Start scanning for Yeelight devices."""
        _LOGGER.debug("Start scanning")
        # Use loop directly to avoid home assistant track this task
        self._scan_task = self._hass.loop.create_task(self._async_scan())

    @callback
    def _async_stop_scan(self):
        """Stop scanning."""
        _LOGGER.debug("Stop scanning")
        if self._scan_task is not None:
            self._scan_task.cancel()
            self._scan_task = None

    @callback
    def async_register_callback(self, unique_id, callback_func):
        """Register callback function."""
        host = self._seen.get(unique_id)
        if host is not None:
            self._hass.async_create_task(callback_func(host))
        else:
            self._callbacks[unique_id] = callback_func
            if len(self._callbacks) == 1:
                self._async_start_scan()

    @callback
    def async_unregister_callback(self, unique_id):
        """Unregister callback function."""
        if unique_id not in self._callbacks:
            return
        self._callbacks.pop(unique_id)
        if len(self._callbacks) == 0:
            self._async_stop_scan()


class YeelightDevice:
    """Represents single Yeelight device."""

    def __init__(self, hass, host, config, bulb, capabilities):
        """Initialize device."""
        self._hass = hass
        self._config = config
        self._host = host
        self._bulb_device = bulb
        self._capabilities = capabilities or {}
        self._device_type = None
        self._available = False
        self._initialized = False

        self._name = host  # Default name is host
        if capabilities:
            # Generate name from model and id when capabilities is available
            self._name = _async_unique_name(capabilities)
        if config.get(CONF_NAME):
            # Override default name when name is set in config
            self._name = config[CONF_NAME]

    @property
    def bulb(self):
        """Return bulb device."""
        return self._bulb_device

    @property
    def name(self):
        """Return the name of the device if any."""
        return self._name

    @property
    def config(self):
        """Return device config."""
        return self._config

    @property
    def host(self):
        """Return hostname."""
        return self._host

    @property
    def available(self):
        """Return true is device is available."""
        return self._available

    @property
    def model(self):
        """Return configured/autodetected device model."""
        return self._bulb_device.model

    @property
    def fw_version(self):
        """Return the firmware version."""
        return self._capabilities.get("fw_ver")

    @property
    def is_nightlight_supported(self) -> bool:
        """
        Return true / false if nightlight is supported.

        Uses brightness as it appears to be supported in both ceiling and other lights.
        """
        return self._nightlight_brightness is not None

    @property
    def is_nightlight_enabled(self) -> bool:
        """Return true / false if nightlight is currently enabled."""
        if self.bulb is None:
            return False

        # Only ceiling lights have active_mode, from SDK docs:
        # active_mode 0: daylight mode / 1: moonlight mode (ceiling light only)
        if self._active_mode is not None:
            return self._active_mode == ACTIVE_MODE_NIGHTLIGHT

        if self._nightlight_brightness is not None:
            return int(self._nightlight_brightness) > 0

        return False

    @property
    def is_color_flow_enabled(self) -> bool:
        """Return true / false if color flow is currently running."""
        return self._color_flow == ACTIVE_COLOR_FLOWING

    @property
    def _active_mode(self):
        return self.bulb.last_properties.get("active_mode")

    @property
    def _color_flow(self):
        return self.bulb.last_properties.get("flowing")

    @property
    def _nightlight_brightness(self):
        return self.bulb.last_properties.get("nl_br")

    @property
    def type(self):
        """Return bulb type."""
        if not self._device_type:
            self._device_type = self.bulb.bulb_type

        return self._device_type

    async def async_turn_on(
        self, duration=DEFAULT_TRANSITION, light_type=None, power_mode=None
    ):
        """Turn on device."""
        try:
            await self.bulb.async_turn_on(
                duration=duration, light_type=light_type, power_mode=power_mode
            )
        except BulbException as ex:
            _LOGGER.error("Unable to turn the bulb on: %s", ex)

    async def async_turn_off(self, duration=DEFAULT_TRANSITION, light_type=None):
        """Turn off device."""
        try:
            await self.bulb.async_turn_off(duration=duration, light_type=light_type)
        except BulbException as ex:
            _LOGGER.error(
                "Unable to turn the bulb off: %s, %s: %s", self._host, self.name, ex
            )

    async def _async_update_properties(self):
        """Read new properties from the device."""
        if not self.bulb:
            return

        try:
            await self.bulb.async_get_properties(UPDATE_REQUEST_PROPERTIES)
            self._available = True
            if not self._initialized:
                await self._async_initialize_device()
        except BulbException as ex:
            if self._available:  # just inform once
                _LOGGER.error(
                    "Unable to update device %s, %s: %s", self._host, self.name, ex
                )
            self._available = False

        return self._available

    async def _async_get_capabilities(self):
        """Request device capabilities."""
        try:
            await self._hass.async_add_executor_job(self.bulb.get_capabilities)
            _LOGGER.debug(
                "Device %s, %s capabilities: %s",
                self._host,
                self.name,
                self.bulb.capabilities,
            )
        except BulbException as ex:
            _LOGGER.error(
                "Unable to get device capabilities %s, %s: %s",
                self._host,
                self.name,
                ex,
            )

    async def _async_initialize_device(self):
        await self._async_get_capabilities()
        self._initialized = True
        async_dispatcher_send(self._hass, DEVICE_INITIALIZED.format(self._host))

    async def async_update(self):
        """Update device properties and send data updated signal."""
        if self._initialized and self._available:
            # No need to poll, already connected
            return
        await self._async_update_properties()
        async_dispatcher_send(self._hass, DATA_UPDATED.format(self._host))

    @callback
    def async_update_callback(self, data):
        """Update push from device."""
        self._available = data.get(KEY_CONNECTED, True)
        async_dispatcher_send(self._hass, DATA_UPDATED.format(self._host))


class YeelightEntity(Entity):
    """Represents single Yeelight entity."""

    def __init__(self, device: YeelightDevice, entry: ConfigEntry) -> None:
        """Initialize the entity."""
        self._device = device
        self._unique_id = entry.entry_id
        if entry.unique_id is not None:
            # Use entry unique id (device id) whenever possible
            self._unique_id = entry.unique_id

    @property
    def unique_id(self) -> str:
        """Return the unique ID."""
        return self._unique_id

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device info."""
        return {
            "identifiers": {(DOMAIN, self._unique_id)},
            "name": self._device.name,
            "manufacturer": "Yeelight",
            "model": self._device.model,
            "sw_version": self._device.fw_version,
        }

    @property
    def available(self) -> bool:
        """Return if bulb is available."""
        return self._device.available

    @property
    def should_poll(self) -> bool:
        """No polling needed."""
        return False

    async def async_update(self) -> None:
        """Update the entity."""
        await self._device.async_update()


async def _async_get_device(
    hass: HomeAssistant, host: str, entry: ConfigEntry
) -> YeelightDevice:
    # Get model from config and capabilities
    model = entry.options.get(CONF_MODEL)

    # Set up device
    bulb = AsyncBulb(host, model=model or None)
    capabilities = await hass.async_add_executor_job(bulb.get_capabilities)

    return YeelightDevice(hass, host, entry.options, bulb, capabilities)
