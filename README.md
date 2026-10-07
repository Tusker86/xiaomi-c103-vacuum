# Xiaomi c103 vacuum app for Home Assistant

A Home Assistant add-on for the **Xiaomi Mijia 3C Enhanced** robot vacuum (`xiaomi.vacuum.c103`).
It controls the robots over your home network, shows the live position and trail on the map, and
gives Home Assistant entities over MQTT.

*Unofficial. Not affiliated with Xiaomi. A robot moves when you tell it to: use it at your own risk.*

## What it does

- **Local control** over your network (no cloud round trip): clean rooms, clean a zone, full clean,
  stop, dock, find (beep), suction, water, mode, repeat, volume.
- **Live map:** the robot's position and trail update about once a second while it moves.
- **Map picture** from the Xiaomi cloud (the robot uploads it, the app downloads and draws it).
  Room names are stored on the robot itself; rename or merge rooms from the page.
- **Consumables:** life left and hours left, with reset buttons.
- **Home Assistant entities** over MQTT: a vacuum, battery, status, fault, cleaning time and area,
  consumables, settings, and a "Clean *room*" button per named room.
- **A web page**, the "Vacuums" entry in the Home Assistant sidebar (no port is opened on your network).
- **No setup of robot details by hand:** give the app your Xiaomi login and it finds your c103 robots
  (name, IP address, token) by itself.

## Requirements

- Home Assistant with add-ons (Home Assistant OS or Supervised), on `aarch64` or `amd64`.
- One or more c103 robots, already set up in the Mi Home app and with a map.
- A Xiaomi account that owns the robots (to find them and download the map picture).
- Optional: the Mosquitto add-on, for the Home Assistant entities.

## Install

1. In Home Assistant: **Settings > Add-ons > Add-on store > ⋮ > Repositories**, add
   `https://github.com/Tusker86/xiaomi-c103-vacuum`.
2. Install **Vacuum App (c103)**.
3. Open its **Configuration** tab and fill in:
   - **Xiaomi login:** the browser cookies `userId` (about 10 digits) and `passToken` from a login at
     account.xiaomi.com. The **Documentation** tab explains how to get them, step by step.
   - **MQTT:** your Mosquitto user (leave the username empty to skip the Home Assistant entities).
   - Leave **Robots** empty: the app finds them.
4. Save, **Start**, and open **Vacuums** in the sidebar.
5. Reserve each robot's IP address in your router so it never changes.

If something is wrong, the **Log** tab says what and what to do.

## How it works

- Control and the live position use the robot's local protocol (UDP, with the robot's IP and token).
  They keep working when Xiaomi's cloud is down.
- Only the map **picture** needs the Xiaomi cloud. The app keeps one saved Xiaomi login for that and
  renews its short-lived keys by itself. If Xiaomi ever cancels the long-lived part, the map stops
  updating (nothing else does) and the page shows "Map download: failing". Paste a fresh `passToken`
  and restart the add-on. Your Xiaomi password is never stored.
- The Xiaomi login, the robot list and the saved maps are kept in the add-on's data folder
  (`/homeassistant/vacuum_app`). Nothing private is in this repository.

## Limits

- Tested with one account in the `cn` region and two robots. Other Xiaomi regions are untried
  (the region setting is `auto`).
- Tested on `aarch64` (Raspberry Pi). The `amd64` build has not been tried.
- Room cleans use the robot's active map; only one saved map per robot is shown on the page.
- Rooms can be merged from the page (a Merge button; it goes through the Xiaomi cloud, and the merged
  room is named again). Building a new map (for example when a robot works on another floor),
  splitting rooms, virtual walls and no-go zones are not in the app yet: do them in Mi Home.
- Schedules, do-not-disturb editing and clean history are not built; use Home Assistant automations.
- Found a problem, or tried another region, an `amd64` machine or a different setup? Please
  [open an issue](https://github.com/Tusker86/xiaomi-c103-vacuum/issues) on GitHub and paste the add-on's
  **Log** tab (check it first for anything private).

## Licence and credits

MIT licence, see [LICENSE](LICENSE). Copyright (c) 2026 Tusker86.

The robot protocol, the map decoding and parts of the code come from
[xiaomi-vac](https://github.com/letitbe-dull/xiaomi-vac), Copyright (c) 2026 letitbe-dull, also MIT;
its notice is in `c103_vacuum/app/third_party/LICENSE-xiaomi-vac.txt` and must be kept with any copy.
