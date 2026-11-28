# Nexxtender BLE for Home Assistant

A Home Assistant custom integration that talks to a **Powerdale Nexxtender Home**
EV charger directly over Bluetooth, through the Bluetooth adapter of the machine
Home Assistant runs on. You don't need an ESP32, MQTT or the cloud.

The protocol knowledge comes from the community reverse-engineering in
[geertmeersman/nexxtender](https://github.com/geertmeersman/nexxtender) (ESPHome)
and [toSvenson/nexxtender-ble](https://github.com/toSvenson/nexxtender-ble).

> **Status: v0.3.5, read-only plus Start/Stop.** Pairing and connection confirmed
> on a real Nexxtender Home (Bluetooth adapter passed through to the HA VM, weak
> signal). To keep things safe while it's being tested, the integration doesn't
> change any setting on the charger: it reads values and can only start or stop
> a charging session. If a value looks wrong, turn on debug logging (see
> Troubleshooting) and keep the raw payloads.

## What you get

| Type | Entities |
|---|---|
| Sensors | Status (unplugged / plugged / charging / fault), session state, session energy (kWh), session duration, car power, car current (total + L1–L3), grid power, grid current L1–L3, grid consumed power, available current, charging phase count, **mode** (Eco/Max, private/open) |
| Binary sensors | Car plugged in, Charging, Bluetooth connected |
| Buttons | Start charging, Stop charging, Refresh configuration |
| Switch | Hold Bluetooth connection (turn off to let the phone app connect; this doesn't touch the charger) |
| Diagnostics | Charger clock, off-peak schedules, max charging speed, max available capacity, offloading minimum, firmware |

Session energy can be added to the **Energy dashboard** as an EV charger consumption.

### What the integration sends to the charger

- **On every connection:** Bluetooth pairing with your PIN, then read requests only
  (status, values, configuration, clock).
- **Every 15 s:** it reads the current values.
- **Start charging / Stop charging buttons:** one command each, only when you
  press them. *Start charging* uses the mode set on the charger (in Eco mode the
  charger may wait for its own off-peak window).

Nothing else is written: no mode change, no current limit, no clock setting. Change
those in the Nexxtmove app (turn off *Hold Bluetooth connection* first).

**Modes:** *Eco* charges only in the off-peak window configured in the charger (and
respects the offloading settings). *Max* charges at full speed. *Private* means a
session must be started (badge, app, or the *Start charging* button). *Open* means
any plugged-in car starts charging.

## 1. Give the Home Assistant VM the laptop's Bluetooth (Proxmox)

Bluetooth has to be visible **inside** the HA VM. On most laptops the Bluetooth part
of the Wi-Fi card sits on the internal USB bus, so you pass it through like a USB stick.

1. In the Proxmox web UI, open **your node → Shell** and run:
   ```bash
   lsusb
   ```
   Look for a line with "Bluetooth", e.g. `ID 8087:0026 Intel Corp. AX201 Bluetooth`
   or `ID 0bda:b85b Realtek Bluetooth Radio`. Note the `xxxx:yyyy` ID.
2. Make sure the Proxmox host itself is not using it (usually it isn't):
   ```bash
   systemctl disable --now bluetooth 2>/dev/null; true
   ```
3. **HA VM → Hardware → Add → USB Device → "Use USB Vendor/Device ID"**, pick the
   Bluetooth device. (Shell equivalent: `qm set <VMID> -usb0 host=8087:0026`.)
4. **Shut down** the VM and start it again (a reboot from inside HA is not enough).
5. In Home Assistant: **Settings → Devices & services → Bluetooth** should now show an
   adapter (`hci0`). If it appears under *Discovered*, add it.

If the adapter never shows up or the logs show firmware errors (`Reading Intel version
command failed`, `Fw download error`), the laptop's chip doesn't cooperate inside a VM.
That's a known problem with some Intel/Realtek combo cards. The fix is a cheap USB
Bluetooth dongle, ideally one from the
[list of adapters recommended by Home Assistant](https://www.home-assistant.io/integrations/bluetooth/#known-working-high-performance-adapters).
Pass the dongle through the same way.

> This integration needs a **local** adapter handled by BlueZ (the laptop's
> Bluetooth or a USB dongle). ESPHome Bluetooth proxies won't work, because the
> charger needs passkey pairing, which proxies can't do.

## 2. Install the integration

**Option A: copy the files.** Copy the `custom_components/nexxtender_ble` folder into
your HA config folder, so you end up with `/config/custom_components/nexxtender_ble/`.
The *Samba share* or *Studio Code Server* add-on is the easiest way. Then restart
Home Assistant.

**Option B: HACS.** In HACS: *⋮ → Custom repositories →*
`https://github.com/jvellemans/nexxtender_ble`, *type Integration*, then install
*Nexxtender BLE* and restart HA. HACS then offers updates whenever a new version is
pushed. For a private repo, HACS needs a GitHub token with access to it.

## 3. Pair and add the charger

1. Find the 6-digit Bluetooth **PIN**. The "Nexxtmove for Home Assistant" HACS
   integration (cloud account) can retrieve it. Otherwise, geertmeersman's README
   explains how to have it calculated from the serial number on the sticker under
   the box.
2. **Close the Nexxtmove app / turn off Bluetooth on your phone.** The charger
   accepts only one Bluetooth connection at a time.
3. *Settings → Devices & services → Add integration → Nexxtender BLE*. Pick the charger
   from the list or type its MAC address. Many chargers advertise **without a name**;
   find the address under *Settings → Devices & services → Bluetooth → Advertisement
   monitor* (it's the device whose signal gets stronger near the charger).
4. Enter the PIN. HA pairs with the charger (up to a minute), reads its serial number
   and creates the device.

## Using the phone app

While HA holds the connection, the phone can't connect. Turn off **Hold Bluetooth
connection**, use the app, then turn the switch back on. HA reconnects within seconds.

## Troubleshooting

**Easiest:** *Settings → Devices & services → Nexxtender BLE → ⋮ → Download diagnostics*.
The JSON contains the raw configuration payload, the off-peak minutes, the last ~40
protocol exchanges and any errors (the PIN and serial are redacted).

For live logs, enable debug logging in `configuration.yaml` and restart:
```yaml
logger:
  default: warning
  logs:
    custom_components.nexxtender_ble: debug
```

- **"Pairing failed" during setup:** check that the Bluetooth adapter belongs to the
  HA VM (step 1) and that no phone is connected to the charger.
- **"The charger asked for the PIN and then refused it":** double-check the 6 digits.
- **"Pairing was interrupted":** the Bluetooth link dropped during pairing, almost
  always a weak signal. Look at the RSSI in *Bluetooth → Advertisement monitor*:
  aim for better than about -80 dBm (move the laptop or use a USB dongle on an
  extension cable).
- Every failed attempt shows the raw BlueZ error under the PIN field and logs it as
  a warning (*Settings → System → Logs*), including which pairing requests the
  charger made ("agent calls").
- **Connected, but values stay unavailable:** the debug log shows every raw payload
  (hex). Please keep those lines; they're what's needed to fix a parsing difference.
- **Reconnecting takes 15–30 seconds:** that's expected. The charger answers
  "Unlikely Error" to anything sent over an unencrypted link, and doesn't seem to keep
  a lasting pairing. So on each new connection the integration checks access, and if
  needed removes the old pairing and pairs again with the PIN, like the ESP32 client
  does on every connection.
- **The charger was reset or re-paired elsewhere:** the integration removes the old
  bond and pairs again by itself. If the PIN changed, HA asks for the new one.
- **Off-peak schedule times look an hour off:** the charger stores them in UTC and
  HA converts them to your local time zone. If they don't match the Nexxtmove app,
  report it, since the exact convention isn't confirmed yet.

## How it works (for the curious)

- `protocol.py`: pure byte encoding/decoding (CRC-16/MODBUS, the legacy 15-byte
  config, the data characteristics). It has no HA imports and is
  fully unit-tested.
- `pairing.py`: HA's Bluetooth stack can only do "Just Works" pairing, and the
  Nexxtender needs a passkey. So the integration briefly registers its own BlueZ
  pairing agent (`org.bluez.Agent1`, *KeyboardOnly*) on the system D-Bus, answers the
  charger's passkey request with your PIN, and lets BlueZ store the bond.
- `client.py`: one persistent BLE connection with notifications (status, grid, car,
  protocol status/data), polling of the read-only characteristic every 15 s, and the
  request/ready/write/success handshakes (config and clock writes exist in the
  client but are not used since 0.3.0).
- `coordinator.py`: reconnects with back-off and pushes
  updates to the entities.

### Running the tests
```bash
pip install pytest-homeassistant-custom-component
pytest          # 43 tests: protocol, D-Bus pairing (needs dbus-daemon), HA entities/flow
```
