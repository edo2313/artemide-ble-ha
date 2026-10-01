# Artemide BLE for Home Assistant

Custom Home Assistant integration for Artemide Bluetooth lamps. It talks to the lamps directly
over Bluetooth Low Energy: no Artemide gateway and no cloud needed. Works with the Home Assistant
host's Bluetooth adapter or with an ESPHome Bluetooth proxy.

> Unofficial project, not affiliated with or endorsed by Artemide. The protocol was
> reverse-engineered for interoperability. Use at your own risk.

## Features

- Automatic discovery of the lamps and of their mesh addresses.
- One light per lamp: on/off, brightness, RGBW color.
- One light per Artemide group (as configured in the official app): a single mesh command switches
  all members at the same time.
- State polled back from the lamps (on/off and color).
- Any lamp in range works as the entry point; the lamps relay commands to the rest of the mesh.
- `artemide_ble.send_commands` diagnostic action to send raw protocol commands.

## Supported lamps

Built for and tested only with the Artemide **Discovery RGBW** (advertised name `A2O...`,
firmware 192).

Other Artemide Bluetooth lamps use the same mesh protocol, so discovery, on/off and polling will
likely work on them too, but they are untested:
- Other RGBW models (e.g. Sharp RGBW, A24 RGBW, Nur, Bespoke RGBW) are the closest match.
- White-only and tunable-white models (Discovery TW, A24 TW, Algo TW...) take different color
  commands, which this integration does not implement: only on/off is expected to work.

Reports from other models are welcome.

## Using the official app

**While this integration is running, the official Artemide app cannot control the lamps.** The
integration keeps a Bluetooth connection open to one lamp, and a lamp accepts only one connection
at a time.

To use the official app again (for example to commission lamps or edit groups):
1. Disable the integration (Settings -> Devices & Services -> Artemide BLE -> menu -> Disable).
2. If the app still cannot connect, power-cycle the lamps (switch the power off and on at the
   mains) so their Bluetooth modules reboot and drop any stale connection.
3. When done, close the app, re-enable the integration and, if you changed lamps or groups, run
   **Re-run lamp discovery** from the integration options.

## Requirements

- Home Assistant 2024.11 or newer.
- A Bluetooth adapter on the Home Assistant host, or an ESP32 running the ESPHome
  [Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) with **active**
  connections enabled, in range of at least one lamp:
  ```yaml
  esp32_ble_tracker:
  bluetooth_proxy:
    active: true
  ```
- Lamps already commissioned with the official Artemide app (the integration reads the network the
  app created; it does not commission lamps or create groups).

## Installation

### HACS

1. HACS -> Integrations -> menu -> **Custom repositories**.
2. Add `https://github.com/edo2313/artemide-ble-ha` with category **Integration**.
3. Install **Artemide BLE** and restart Home Assistant.

### Manual

Copy `custom_components/artemide_ble/` into your Home Assistant `config/custom_components/` folder and
restart Home Assistant.

## Configuration

Close the official app first (see [Using the official app](#using-the-official-app)).

1. Settings -> Devices & Services -> **Add Integration** -> **Artemide BLE** (or accept the
   discovered device).
2. Enter the network password. The default (`00000@lLiX`) is the one the official app uses
   unless you changed it.
3. The integration connects to each lamp in range once to read its mesh address and groups, then
   looks for lamps reachable only through the mesh.
4. Confirm the list. Node addresses can also be added by hand (4 characters, e.g. `0004`).

Options (integration -> **Configure**): poll interval, extra node addresses, and **Re-run lamp
discovery** after adding lamps with the official app.

Each light exposes `mesh_node`, `advertised_name`, `device_type` and `groups` as attributes; group
lights expose `group_address`, `members` and `device_types`.

## Diagnostic action

`artemide_ble.send_commands` connects to one lamp, sends raw commands and returns every reply. The
integration is paused while it runs. Write `\r` for a carriage return; prefix `hex:` to send raw
bytes.

```yaml
action: artemide_ble.send_commands
data:
  address: "AA:BB:CC:DD:EE:FF"
  commands:
    - "RID\r"
    - "SMS LNFFFFRF100"
  window: 3
```

Commands starting with `R` only read. `W*`, `C*`, `GS` and `S*` commands change lamp state or
configuration.

## Protocol notes

GATT: service and write characteristic `11960002-b656-22ae-e611-9680866c0d6a`, notifications on
`11960003-b656-22ae-e611-9680866c0d6a`. Commands are ASCII, mesh commands are not terminated.

| Command | Meaning |
|---|---|
| `SPS <password>\r` | password handshake after connecting |
| `RID\r` | mesh address of the connected lamp, reply `0001\r` |
| `SMS LN<node>RF100` | output state, reply `RMS <node> 00001\r` (on) / `00000` (off) |
| `SMS LN<node>RC` | color, reply `RMS <node> ` + 4 words W,R,G,B + `\r` |
| `SMS LN<node>RS105` | group slots, reply `RMS <node> 1111 \r` (4 slots, `0` = empty) |
| `SMS LN<node>WF100<0\|1>` | lamp off/on |
| `SMS LN<node>WC` + 4 words | lamp color W,R,G,B |
| `SMS LG<gggg>WF<ch><0\|1>` | group off/on, `<gggg>` = group id x4, `<ch>` from device type (A=102 ... S=118, no J/K) |
| `SMS LG<gggg>C<type>` + 4 words | group color, `<type>` = device type letter in lower case |
| `SMS LNFFFF...` | broadcast to every node |

Color word: `((value * 1000 / 255) << 5) | 0x801F`, big-endian. Mesh replies start with
`RMS <node> `; a bare `ERR\r` means the connected lamp rejected the command (the integration
reconnects). The device type letter is character 8 of the advertised name, case-insensitive
(`A2O1234d...` -> `D`).

## Troubleshooting

Enable debug logging to see every command (`TX`) and reply (`RX`):

```yaml
logger:
  logs:
    custom_components.artemide_ble: debug
```

- **The official app cannot connect:** see [Using the official app](#using-the-official-app).
- **No lamp found:** check that the proxy is online, has active connections enabled, and that the
  official app is not connected to the lamps.
- **No lamp answered during setup:** wrong network password, or the lamps are out of range.
- **A group light is missing:** groups are read from the lamps at setup; re-run discovery.

## Limitations

- Scenes and dynamic scenes of the official app are not supported.
- Groups and lamps cannot be created or edited from Home Assistant.
- RGB-only lamps share the device type letter of RGBW ones, so they also show a white channel.

## License

[MIT](LICENSE)
