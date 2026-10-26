# Maintenance and troubleshooting

## Update Lucy Agenda

For a Git checkout, pull the desired release, copy the updated files to `~/.local/share/lucy-ticktick-widget/`, then restart the service:

```bash
systemctl --user restart lucy-ticktick-widget.service
```

Check it is running with:

```bash
systemctl --user status lucy-ticktick-widget.service
```

## Where settings live

The widget stores window position, size, opacity, visible-calendar choices and notification preferences in:

`~/.config/lucy-ticktick-widget/config.json`

TickTick’s access token is in GNOME Keyring. Active task recovery data and notification history are also kept under `~/.config/lucy-ticktick-widget/`. None of these files are tracked by Git.

## Common problems

**TickTick says authorisation expired** — run `setup_gui.py` again and complete TickTick authorisation.

**Calendar is missing** — check that Calendar is enabled for the Google account under GNOME Online Accounts, then restart Lucy Agenda.

**A calendar is absent from the widget** — open **⋯ → Visible calendars** and make sure it is ticked.

**The widget is hidden** — launch Lucy Agenda from the app menu or restart the service. The × button hides the window by design.

**The list says Offline** — confirm the internet connection, then use the refresh button. The widget retries automatically every 15 seconds while either source is unreachable.

## Reporting an issue

Include the Ubuntu version, Lucy Agenda version, whether the issue affects TickTick or Calendar, and the recent service log:

```bash
journalctl --user -u lucy-ticktick-widget.service -n 80 --no-pager
```
