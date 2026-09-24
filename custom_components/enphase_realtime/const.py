"""Constants for the Enphase Realtime integration."""

from datetime import timedelta

from homeassistant.const import UnitOfTemperature

DOMAIN = "enphase_realtime"

# --- Config entry data (set by the config flow) ---------------------------------------------------
CONF_SERIAL = "serial"
CONF_FIRMWARE = "firmware"
CONF_SITE_ID = "site_id"
CONF_TOKEN = "token"
CONF_PHASE_LAYOUT = "phase_layout"
CONF_HAS_BATTERY = "has_battery"
CONF_HAS_ENPOWER = "has_enpower"

# --- Options (spec 4.3) ---------------------------------------------------------------------------
CONF_FAST_INTERVAL = "fast_interval"
CONF_STREAM_INTERVAL = "stream_interval"
CONF_CLOUD_INTERVAL = "cloud_interval"
CONF_ENABLE_STREAM = "enable_stream"
CONF_COUNTRY = "country"
CONF_TIME_ZONE = "time_zone"

DEFAULT_HOST = "envoy.local"
DEFAULT_FAST_INTERVAL = 5
DEFAULT_STREAM_INTERVAL = 5
DEFAULT_CLOUD_INTERVAL = 300
DEFAULT_ENABLE_STREAM = True

SLOW_INTERVAL = timedelta(seconds=60)
# Stream entities go unavailable after this long without a frame (spec 3.1).
STREAM_STALE_AFTER = timedelta(seconds=30)
# How long setup waits for the first stream frame before creating stream entities anyway.
STREAM_PROBE_TIMEOUT = 15.0
# Renew the owner token this long before it expires (spec 4.2).
TOKEN_RENEW_WINDOW = timedelta(days=30)

MIN_FIRMWARE_MAJOR = 7

# Each device reports temperature in its own unit (spec 5.3). Verified on the reference system
# only; other System Controller models may differ.
BATTERY_TEMPERATURE_UNIT = UnitOfTemperature.CELSIUS
ENPOWER_TEMPERATURE_UNIT = UnitOfTemperature.FAHRENHEIT

# Entity names use the installer's terms; unique IDs keep the Envoy's keys (spec 3.3).
PHASE_NAMES = {"ph-a": "L1", "ph-b": "L2", "ph-c": "L3"}
