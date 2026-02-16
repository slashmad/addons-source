#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2019       Paul Culley <paulr2787_at_gmail.com>
# Copyright (C) 2026       stolpee
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
"""Loader for Themes preferences extension."""

import os
import sys

from gi.repository import GLib


def _bool_from_value(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_on_reg(dbstate, uistate, plugin):
    """Runs when plugin is registered."""
    if not uistate:
        # Avoid loading GUI elements in CLI mode.
        return

    from gi.repository.Gdk import Screen
    from gi.repository.Gtk import (
        CssProvider,
        Settings,
        STYLE_PROVIDER_PRIORITY_APPLICATION,
        StyleContext,
    )

    from gramps.gen.config import config
    from gramps.gui.configure import GrampsPreferences

    sys.path.append(os.path.abspath(os.path.dirname(__file__)))
    from themes import (
        KEY_FIXED_SCROLL,
        KEY_FONT,
        MyPrefs,
        apply_theme_settings,
        register_config_defaults,
        setup_system_darkmode_listener,
    )

    register_config_defaults()

    gtksettings = Settings.get_default()
    if gtksettings and not hasattr(GrampsPreferences, "def_dark"):
        GrampsPreferences.def_dark = gtksettings.get_property(
            "gtk-application-prefer-dark-theme"
        )
        GrampsPreferences.def_theme = gtksettings.get_property("gtk-theme-name")
        GrampsPreferences.def_font = gtksettings.get_property("gtk-font-name")

    def _apply_patch():
        GrampsPreferences.__init__ = MyPrefs.__init__
        apply_theme_settings()

        if gtksettings:
            font_value = config.get(KEY_FONT)
            if font_value:
                gtksettings.set_property("gtk-font-name", font_value)

            fixed_scroll = _bool_from_value(config.get(KEY_FIXED_SCROLL), False)
            if fixed_scroll:
                gtksettings.set_property("gtk-primary-button-warps-slider", False)
                MyPrefs.provider = CssProvider()
                css = (
                    "* { -GtkScrollbar-has-backward-stepper: 1; "
                    "-GtkScrollbar-has-forward-stepper: 1; }"
                )
                MyPrefs.provider.load_from_data(css.encode("utf8"))
                StyleContext.add_provider_for_screen(
                    Screen.get_default(),
                    MyPrefs.provider,
                    STYLE_PROVIDER_PRIORITY_APPLICATION,
                )
        return False

    _apply_patch()
    GLib.idle_add(_apply_patch)
    GLib.timeout_add(1000, _apply_patch)
    setup_system_darkmode_listener()
