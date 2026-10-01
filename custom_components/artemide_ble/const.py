"""Constants for the Artemide BLE integration."""

from __future__ import annotations

DOMAIN = "artemide_ble"

# GATT layout. NOTE: the write/command characteristic
# UUID equals the service UUID in the vendor app; notify is the ...0003 char.
SERVICE_UUID = "11960002-b656-22ae-e611-9680866c0d6a"
WRITE_CHAR_UUID = "11960002-b656-22ae-e611-9680866c0d6a"
NOTIFY_CHAR_UUID = "11960003-b656-22ae-e611-9680866c0d6a"

# Advertised names: every Artemide mesh device advertises "A<model><...>" ending
# in its MAC ("..XX:XX:XX:XX:XX:XX"), like the official app filters. Char 7 of the
# name is the device type letter (A..S) used by group WF opcodes.
NAME_PREFIX = "A"
NAME_TYPE_INDEX = 7

# Color word: ((value * 1000 / 255) << 5) | COLOR_FLAG. COLOR_FLAG == 32799.
COLOR_FLAG = 0x801F

# Default network password used by the official app: "SPS 00000@lLiX\r".
DEFAULT_PASSWORD = "00000@lLiX"

# Config / options keys.
CONF_PASSWORD = "password"
CONF_LAMPS = "lamps"
CONF_EXTRA_NODES = "extra_nodes"
CONF_REDISCOVER = "rediscover"
CONF_POLL_INTERVAL = "poll_interval"

# Keys of one lamp record in entry.data[CONF_LAMPS].
LAMP_NODE = "node"  # 4-char mesh address (from RID / manual)
LAMP_MAC = "mac"  # BLE address, None for nodes only reachable through the mesh
LAMP_NAME = "name"  # advertised name
LAMP_TYPE = "type"  # device type letter, None if unknown
LAMP_GROUPS = "groups"  # group ids read from register 105, [] if unknown

DEFAULT_POLL_INTERVAL = 30  # seconds

# Protocol timing (seconds), matching the official app.
WAIT_NEXT_COMMAND = 0.2
WAIT_READ_RF = 0.3
WAIT_READ_RC = 0.5

# Timeout waiting for a notify response to a read command.
READ_TIMEOUT = 3.0

# Per-lamp identify (config flow): connect to each advertising lamp, read its
# own mesh address with RID. Connection attempt budget per lamp.
IDENTIFY_TIMEOUT = 20.0

# Broadcast read "SMS LNFFFFRF100": collect replies for this long. Each mesh
# reply carries the sender address in bytes 4..7. Unverified on hardware.
BROADCAST_WINDOW = 3.0

# Node probe-scan (fallback for lamps out of direct BLE range): sweep 0001..
# sending RF100, treat any reply as "node exists". Addresses are dense hex
# assigned max+1 by the vendor app, so a sequential probe finds them.
SCAN_TIMEOUT = 1.0
SCAN_MAX_COUNT = 32  # highest node index to probe (0x0001..)
SCAN_MAX_GAP = 4  # stop after this many consecutive non-responses (once >=1 found)
