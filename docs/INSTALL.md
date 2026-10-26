# Installation and setup

## Before you begin

Lucy Agenda needs Ubuntu with a GNOME desktop session, Python 3, the `requests` Python package, a TickTick developer application and a Google account connected under **Settings → Online Accounts** with Calendar enabled.

## Install the app

From the downloaded source folder, copy the application to the location used by the launcher and service:

```bash
install -d ~/.local/share/lucy-ticktick-widget
cp app.py launch.sh oauth_setup.py setup_gui.py ~/.local/share/lucy-ticktick-widget/
install -Dm644 lucy-ticktick-widget.service ~/.config/systemd/user/lucy-ticktick-widget.service
install -Dm644 ticktick-widget.desktop ~/.local/share/applications/ticktick-widget.desktop
systemctl --user daemon-reload
systemctl --user enable --now lucy-ticktick-widget.service
```

The service is the startup mechanism. It waits briefly for the desktop and network, then starts Lucy Agenda when you sign in.

## Connect TickTick

Run the setup window once:

```bash
python3 ~/.local/share/lucy-ticktick-widget/setup_gui.py
```

Enter the Client ID and Client Secret from your TickTick developer application, then complete the browser authorisation. The token is stored in GNOME Keyring; it is not written into the source directory.

## Connect Google Calendar

Open **Settings → Online Accounts**, add or select your Google account, and make sure Calendar is enabled. Restart Lucy Agenda after connecting it. The widget uses that GNOME account connection directly, so it does not need a second Google password or a copied access token.

## Start, hide and stop

- Open **Lucy Agenda** from the application menu to restart it if it was hidden.
- The × button hides the window without stopping the background service.
- To stop it for the current session: `systemctl --user stop lucy-ticktick-widget.service`.
- To prevent it starting next sign-in: `systemctl --user disable lucy-ticktick-widget.service`.
