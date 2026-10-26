#!/bin/sh
# Restarting the managed service also brings back a widget hidden with ×,
# while preventing duplicate widget processes.
exec /usr/bin/systemctl --user restart lucy-ticktick-widget.service
