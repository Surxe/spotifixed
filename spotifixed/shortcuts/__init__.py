"""``shortcuts``: global keyboard shortcuts that work while another window has focus.

A client of the daemon's HTTP API, like the overlay. It registers its shortcuts with the
desktop's global shortcuts portal and turns each press into one API request. The volume
shortcuts and skip-and-turn-off live here: play, pause, next and previous are the
keyboard's media keys, which reach mpv through its MPRIS plugin. The shortcuts
themselves are defined in ``shortcut_settings``, which the daemon shares; only ``portal``
needs the system GObject bindings. Like the overlay, nothing here may import the cache,
the player or the daemon.
"""
