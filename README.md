# Lucy Agenda

**Lucy Agenda v1.0.0** is a small, low-resource desktop widget for Ubuntu. It combines active TickTick tasks and Google Calendar events in one resizable, movable dark-mode window.

## What it does

- Shows overdue, today, unscheduled and upcoming TickTick tasks.
- Shows editable Google Calendar events alongside tasks.
- Lets you complete, create, reschedule, reprioritise, move and delete TickTick tasks.
- Lets you create, reschedule, edit and delete Google Calendar events.
- Includes Today-only and Focus views, a next-event countdown, reminders, pull-to-refresh and smooth scrolling.
- Starts automatically when Lucy signs in and remembers size, location, opacity and always-on-top preference.

## Requirements

- Ubuntu with a GNOME desktop session.
- Python 3 and `requests`.
- A TickTick developer application for OAuth setup.
- A Google Calendar account already connected in GNOME Online Accounts.

## Install

Copy the folder to `~/.local/share/lucy-ticktick-widget`, then install and enable the supplied user service:

```bash
install -Dm644 lucy-ticktick-widget.service ~/.config/systemd/user/lucy-ticktick-widget.service
systemctl --user daemon-reload
systemctl --user enable --now lucy-ticktick-widget.service
```

Run `setup_gui.py` once to connect TickTick. Google Calendar uses the existing GNOME Online Accounts connection.

## Versioning

Lucy Agenda uses semantic versioning. `v1.0.0` is the first public release: TickTick tasks, Google Calendar, editable events, quick-add, task controls, Focus and Today views, notifications, smooth scrolling and startup support.

## Privacy

Tokens and the widget’s local state are stored in the user’s GNOME Keyring and configuration directory. They are not part of this repository.
