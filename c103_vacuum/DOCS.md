# Vacuum App (c103)

Controls Xiaomi Mijia 3C Enhanced robots (`xiaomi.vacuum.c103`) over your home network: clean rooms or a
zone, live position and trail, settings, and Home Assistant entities over MQTT. The web page is the
"Vacuums" entry in the sidebar. If something does not work, read the **Log** tab: the app says what is
wrong and what to do.

## First setup

1. **Robots.** Each robot must already be set up in the Mi Home app and have a map.
2. **Xiaomi login** (lets the app find your robots and download their map picture). Follow
   "Getting the Xiaomi login" below and paste the two values into *Xiaomi login, user id* and
   *Xiaomi login, pass token*. Leave *Robots* empty: the app finds every c103 on your account.
   Without a Xiaomi login, fill in *Robots* by hand (name, IP address, token) and there is no map picture.
3. **MQTT** (Home Assistant entities): install the Mosquitto add-on, create a user for it, and enter that
   user in *MQTT*. Leave the username empty to skip Home Assistant entities.
4. Save, then restart the app. The first start asks Xiaomi for your robots; after that the list is kept
   and the app starts without Xiaomi.
5. Reserve each robot's IP address in your router, so it never changes.
6. Open **Vacuums** in the sidebar. A new map has no room names: select a room, press **Rename**, and the
   name is saved on the robot.

The app is tested with an account in region `cn`; other regions are untried. Leave *Xiaomi region* on `auto`.

## Getting the Xiaomi login

1. In your normal browser, open https://account.xiaomi.com and log in (do the captcha / email code if asked).
2. Press F12, open the **Application** tab (Chrome/Edge) or **Storage** tab (Firefox), then **Cookies**,
   then `https://account.xiaomi.com`.
3. Copy the value of the cookie **userId** (a number of about 10 digits; not **cUserId**, which is a longer
   code) and of the cookie **passToken** (starts with `V1:`, about 350 characters; copy all of it).
4. Paste them into *Xiaomi login, user id* and *Xiaomi login, pass token*, save and restart the app.
   The page should show **Map download: OK** within a minute. The pasted values are used once; you can
   leave them there or clear them.

## Renewing the Xiaomi login

The app renews its short-lived keys by itself. Only the long-lived pass token can be cancelled by Xiaomi.
When that happens the map picture stops updating (nothing else does), the page shows **Map download:
failing** and Home Assistant gets a "Xiaomi map login problem" sensor that turns on. Then repeat
"Getting the Xiaomi login"; only the pass token is needed (the user id never changes).

If the log says Xiaomi did not accept the pasted login, copy the whole value again right after logging in.

## Settings

- **Robots:** normally empty. Fill in only for a robot that is not on your Xiaomi account, or to set its IP.
- **MQTT:** the broker the app publishes the Home Assistant entities to (`core-mosquitto`, port 1883).
