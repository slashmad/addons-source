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
"""Themes preferences panel with dark-mode controls and font settings."""

import glob
import os
import subprocess
import types

from gi.repository import Gio, GLib, Gtk, Pango
from gi.repository.Gdk import Screen
from gi.repository.GObject import BindingFlags

from gramps.gen.config import config
from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.constfunc import win
from gramps.gen.utils.alive import update_constants
from gramps.gui.configure import (
    ConfigureDialog,
    GrampsPreferences,
    WIKI_HELP_PAGE,
    WIKI_HELP_SEC,
)
from gramps.gui.display import display_help

try:
    _trans = glocale.get_addon_translator(__file__)
except ValueError:
    _trans = glocale.translation
_ = _trans.gettext

MODE_AUTO = "auto"
MODE_DARK = "dark"
MODE_LIGHT = "light"
VALID_MODES = {MODE_AUTO, MODE_DARK, MODE_LIGHT}

KEY_MODE = "preferences.theme-mode"
KEY_THEME_DARK = "preferences.theme-dark"
KEY_THEME_LIGHT = "preferences.theme"
KEY_THEME_DARK_VARIANT = "preferences.theme-dark-variant"
KEY_APPLY_THEME = "preferences.theme-apply-name"
KEY_APPLY_CSS_FIXES = "preferences.theme-apply-css-fixes"
KEY_FONT = "preferences.font"
KEY_FIXED_SCROLL = "interface.fixed-scrollbar"

DEFAULT_THEME_DARK = "Adwaita-dark"
DEFAULT_THEME_LIGHT = "Adwaita"

CSS_FIXES = """
/* Keep text widgets readable if theme CSS is incomplete */
textview text {
  background-color: @theme_base_color;
  color: @theme_text_color;
}

entry, textview {
  caret-color: @theme_text_color;
}
"""


class _State:
    css_provider = None
    gnome_interface_settings = None
    gnome_handler_id = None


def register_config_defaults():
    config.register(KEY_MODE, "")
    config.register(KEY_THEME_DARK, DEFAULT_THEME_DARK)
    config.register(KEY_THEME_LIGHT, DEFAULT_THEME_LIGHT)
    config.register(KEY_THEME_DARK_VARIANT, "")
    config.register(KEY_APPLY_THEME, "True")
    config.register(KEY_APPLY_CSS_FIXES, "True")
    config.register(KEY_FONT, "")
    config.register(KEY_FIXED_SCROLL, "0")


def _bool_from_value(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _bool_from_config(key, default=False):
    return _bool_from_value(config.get(key), default)


def _is_flatpak_runtime():
    return os.path.exists("/.flatpak-info")


def _gtk_theme_override_value():
    value = os.environ.get("GTK_THEME")
    if not value:
        return ""
    return value.strip()


def _gtk_theme_override_active():
    return bool(_gtk_theme_override_value())


def _portal_system_prefers_dark():
    """
    Read desktop color-scheme via the XDG portal.
    Returns True/False when available, otherwise None.
    """
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        result = bus.call_sync(
            "org.freedesktop.portal.Desktop",
            "/org/freedesktop/portal/desktop",
            "org.freedesktop.portal.Settings",
            "Read",
            GLib.Variant(
                "(ss)",
                ("org.freedesktop.appearance", "color-scheme"),
            ),
            GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        value = result.unpack()[0]
        # Older PyGObject returns GLib.Variant, newer can return unpacked int.
        if hasattr(value, "unpack"):
            value = value.unpack()

        # https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.impl.portal.Settings.html
        # 0 = no preference, 1 = prefer dark, 2 = prefer light.
        if value == 1:
            return True
        if value == 2:
            return False
    except Exception:
        pass
    return None


def _get_mode():
    value = config.get(KEY_MODE)
    if value:
        value = str(value).strip().lower()
        if value in VALID_MODES:
            return value

    # Legacy compatibility: old Themes addon used only theme-dark-variant bool.
    if config.get(KEY_THEME_DARK_VARIANT) not in (None, ""):
        return (
            MODE_DARK
            if _bool_from_config(KEY_THEME_DARK_VARIANT, False)
            else MODE_LIGHT
        )

    return MODE_AUTO


def _gnome_system_prefers_dark():
    portal_pref = _portal_system_prefers_dark()
    if portal_pref is not None:
        return portal_pref

    schema = Gio.SettingsSchemaSource.get_default()
    if not schema:
        return None

    interface_schema = schema.lookup("org.gnome.desktop.interface", True)
    if not interface_schema or not interface_schema.has_key("color-scheme"):
        return None

    settings = Gio.Settings.new("org.gnome.desktop.interface")
    value = settings.get_string("color-scheme")
    if value == "prefer-dark":
        return True
    if value == "prefer-light":
        return False
    return None


def _fallback_prefers_dark_from_theme_name():
    settings = Gtk.Settings.get_default()
    if not settings:
        return False
    theme_name = settings.get_property("gtk-theme-name") or ""
    return "dark" in str(theme_name).lower()


def _effective_dark_preference():
    mode = _get_mode()
    if mode == MODE_DARK:
        return True
    if mode == MODE_LIGHT:
        return False

    system_pref = _gnome_system_prefers_dark()
    if system_pref is not None:
        return system_pref

    return _fallback_prefers_dark_from_theme_name()


def _collect_theme_names():
    names = set()

    try:
        for theme in Gio.resources_enumerate_children("/org/gtk/libgtk/theme", 0):
            if theme.endswith("/"):
                names.add(theme[:-1])
            elif theme.startswith(("HighContrast", "Raleigh", "gtk-win32")):
                names.add(theme.replace(".css", ""))
    except Exception:
        pass

    theme_roots = [
        os.path.join(os.path.expanduser("~"), ".themes"),
        os.path.join(os.path.expanduser("~"), ".local", "share", "themes"),
    ]

    for root in theme_roots:
        if not os.path.isdir(root):
            continue
        for css_file in glob.glob(os.path.join(root, "*", "gtk-3.*", "gtk.css")):
            names.add(css_file.replace("\\", "/").split("/")[-3])

    for data_dir in GLib.get_system_data_dirs():
        theme_dir = os.path.join(data_dir, "themes")
        if not os.path.isdir(theme_dir):
            continue
        for css_file in glob.glob(os.path.join(theme_dir, "*", "gtk-3.*", "gtk.css")):
            names.add(css_file.replace("\\", "/").split("/")[-3])

    return names


def _resolve_theme_name(prefer_dark):
    configured = config.get(KEY_THEME_DARK if prefer_dark else KEY_THEME_LIGHT) or ""
    configured = configured.strip()
    available = _collect_theme_names()

    if not available:
        return None

    if configured and configured in available:
        return configured

    if prefer_dark:
        if DEFAULT_THEME_DARK in available:
            return DEFAULT_THEME_DARK

        dark_variants = sorted(
            [name for name in available if "dark" in name.lower()],
            key=str.casefold,
        )
        if dark_variants:
            return dark_variants[0]

        # Flatpak runtimes often expose only Adwaita.
        if DEFAULT_THEME_LIGHT in available:
            return DEFAULT_THEME_LIGHT

        return sorted(available, key=str.casefold)[0]

    if DEFAULT_THEME_LIGHT in available:
        return DEFAULT_THEME_LIGHT

    return sorted(available, key=str.casefold)[0]


def _apply_css_fixes(enabled):
    try:
        from gi.repository import Gdk

        screen = Gdk.Screen.get_default()
    except Exception:
        screen = None

    if not screen:
        return

    if _State.css_provider is not None:
        Gtk.StyleContext.remove_provider_for_screen(screen, _State.css_provider)
        _State.css_provider = None

    if not enabled:
        return

    provider = Gtk.CssProvider()
    provider.load_from_data(CSS_FIXES.encode("utf8"))
    Gtk.StyleContext.add_provider_for_screen(
        screen,
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    _State.css_provider = provider


def apply_theme_settings():
    register_config_defaults()

    gtk_settings = Gtk.Settings.get_default()
    if not gtk_settings:
        return

    prefer_dark = _effective_dark_preference()
    config.set(KEY_THEME_DARK_VARIANT, str(prefer_dark))
    gtk_settings.set_property("gtk-application-prefer-dark-theme", prefer_dark)

    if not _gtk_theme_override_active() and _bool_from_config(KEY_APPLY_THEME, True):
        theme_name = _resolve_theme_name(prefer_dark)
        if theme_name:
            gtk_settings.set_property("gtk-theme-name", theme_name)

    _apply_css_fixes(_bool_from_config(KEY_APPLY_CSS_FIXES, True))


def _on_gnome_color_scheme_changed(*_args):
    if _get_mode() == MODE_AUTO:
        apply_theme_settings()


def setup_system_darkmode_listener():
    if _State.gnome_handler_id is not None:
        return

    schema = Gio.SettingsSchemaSource.get_default()
    if not schema:
        return

    interface_schema = schema.lookup("org.gnome.desktop.interface", True)
    if not interface_schema or not interface_schema.has_key("color-scheme"):
        return

    settings = Gio.Settings.new("org.gnome.desktop.interface")
    _State.gnome_handler_id = settings.connect(
        "changed::color-scheme", _on_gnome_color_scheme_changed
    )
    _State.gnome_interface_settings = settings


class MyPrefs(GrampsPreferences):
    """Adds theme, dark-mode, and font controls in Preferences."""

    def __init__(self, uistate, dbstate):
        self.add_themes_panel = types.MethodType(MyPrefs.add_themes_panel, self)
        self.mode_changed = types.MethodType(MyPrefs.mode_changed, self)
        self.theme_dark_changed = types.MethodType(MyPrefs.theme_dark_changed, self)
        self.theme_light_changed = types.MethodType(MyPrefs.theme_light_changed, self)
        self.apply_theme_toggled = types.MethodType(MyPrefs.apply_theme_toggled, self)
        self.css_fixes_toggled = types.MethodType(MyPrefs.css_fixes_toggled, self)
        self.dark_variant_changed = types.MethodType(MyPrefs.dark_variant_changed, self)
        self.apply_now_clicked = types.MethodType(MyPrefs.apply_now_clicked, self)
        self.default_clicked = types.MethodType(MyPrefs.default_clicked, self)
        self.scroll_changed = types.MethodType(MyPrefs.scroll_changed, self)
        self.font_changed = types.MethodType(MyPrefs.font_changed, self)
        self.font_filter = types.MethodType(MyPrefs.font_filter, self)
        self._combo_value = types.MethodType(MyPrefs._combo_value, self)
        self._set_combo_value = types.MethodType(MyPrefs._set_combo_value, self)
        self._sync_dark_toggle = types.MethodType(MyPrefs._sync_dark_toggle, self)

        if hasattr(self, "add_ptypes_panel"):
            page_funcs = (
                self.add_data_panel,
                self.add_general_panel,
                self.add_famtree_panel,
                self.add_import_panel,
                self.add_limits_panel,
                self.add_color_panel,
                self.add_symbols_panel,
                self.add_idformats_panel,
                self.add_text_panel,
                self.add_warnings_panel,
                self.add_researcher_panel,
                self.add_ptypes_panel,
                self.add_themes_panel,
            )
            ConfigureDialog.__init__(
                self,
                uistate,
                dbstate,
                page_funcs,
                GrampsPreferences,
                config,
                on_close=self._close,
            )
        else:
            page_funcs = (
                self.add_data_panel,
                self.add_general_panel,
                self.add_famtree_panel,
                self.add_import_panel,
                self.add_limits_panel,
                self.add_color_panel,
                self.add_symbols_panel,
                self.add_idformats_panel,
                self.add_text_panel,
                self.add_warnings_panel,
                self.add_researcher_panel,
                self.add_themes_panel,
            )
            ConfigureDialog.__init__(
                self,
                uistate,
                dbstate,
                page_funcs,
                GrampsPreferences,
                config,
                on_close=update_constants,
            )

        help_btn = self.window.add_button(_("_Help"), Gtk.ResponseType.HELP)
        help_btn.connect(
            "clicked", lambda _x: display_help(WIKI_HELP_PAGE, WIKI_HELP_SEC)
        )
        self.setup_configs("interface.grampspreferences", 760, 500)

    def add_themes_panel(self, configdialog):
        register_config_defaults()

        grid = Gtk.Grid()
        grid.set_border_width(12)
        grid.set_column_spacing(6)
        grid.set_row_spacing(6)

        row = 0

        info = Gtk.Label(
            label=_(
                "Use Auto to follow GNOME system dark mode. "
                "Use Dark/Light to force a specific mode."
            )
        )
        info.set_halign(Gtk.Align.START)
        info.set_line_wrap(True)
        grid.attach(info, 0, row, 3, 1)
        row += 1

        mode_label = Gtk.Label(label=_("Mode:"))
        mode_label.set_halign(Gtk.Align.START)
        grid.attach(mode_label, 0, row, 1, 1)

        self.mode_combo = Gtk.ComboBoxText()
        self.mode_combo.append(MODE_AUTO, _("Auto (follow system)"))
        self.mode_combo.append(MODE_DARK, _("Dark"))
        self.mode_combo.append(MODE_LIGHT, _("Light"))
        self.mode_combo.set_active_id(_get_mode())
        self.mode_combo.connect("changed", self.mode_changed)
        grid.attach(self.mode_combo, 1, row, 2, 1)
        row += 1

        theme_names = sorted(_collect_theme_names(), key=str.casefold)

        dark_theme_label = Gtk.Label(label=_("Dark theme:"))
        dark_theme_label.set_halign(Gtk.Align.START)
        grid.attach(dark_theme_label, 0, row, 1, 1)

        self.dark_theme_combo = Gtk.ComboBoxText.new_with_entry()
        for name in theme_names:
            self.dark_theme_combo.append(name, name)
        dark_theme = config.get(KEY_THEME_DARK) or DEFAULT_THEME_DARK
        self._set_combo_value(self.dark_theme_combo, dark_theme)
        self.dark_theme_combo.connect("changed", self.theme_dark_changed)
        grid.attach(self.dark_theme_combo, 1, row, 2, 1)
        row += 1

        light_theme_label = Gtk.Label(label=_("Light theme:"))
        light_theme_label.set_halign(Gtk.Align.START)
        grid.attach(light_theme_label, 0, row, 1, 1)

        self.light_theme_combo = Gtk.ComboBoxText.new_with_entry()
        for name in theme_names:
            self.light_theme_combo.append(name, name)
        light_theme = config.get(KEY_THEME_LIGHT) or DEFAULT_THEME_LIGHT
        self._set_combo_value(self.light_theme_combo, light_theme)
        self.light_theme_combo.connect("changed", self.theme_light_changed)
        grid.attach(self.light_theme_combo, 1, row, 2, 1)
        row += 1

        self.apply_theme_check = Gtk.CheckButton(label=_("Apply GTK theme name"))
        self.apply_theme_check.set_active(_bool_from_config(KEY_APPLY_THEME, True))
        self.apply_theme_check.connect("toggled", self.apply_theme_toggled)
        grid.attach(self.apply_theme_check, 0, row, 3, 1)
        row += 1

        self.css_fixes_check = Gtk.CheckButton(label=_("Apply compatibility CSS fixes"))
        self.css_fixes_check.set_active(_bool_from_config(KEY_APPLY_CSS_FIXES, True))
        self.css_fixes_check.connect("toggled", self.css_fixes_toggled)
        grid.attach(self.css_fixes_check, 0, row, 3, 1)
        row += 1

        self.dark = Gtk.CheckButton(label=_("Dark variant (manual override)"))
        self.dark.connect("toggled", self.dark_variant_changed)
        grid.attach(self.dark, 0, row, 3, 1)
        row += 1

        if _gtk_theme_override_active():
            value = _gtk_theme_override_value()
            if _is_flatpak_runtime():
                warning_text = (
                    _(
                        "GTK_THEME is set to '%s'. Theme-name changes from addon are disabled. "
                        "If set by Flatpak override, run:\n"
                        "flatpak override --user --unset-env=GTK_THEME org.gramps_project.Gramps"
                    )
                    % value
                )
            else:
                warning_text = (
                    _(
                        "GTK_THEME is set to '%s'. Theme-name changes from addon are disabled."
                    )
                    % value
                )
            warning = Gtk.Label(label=warning_text)
            warning.set_halign(Gtk.Align.START)
            warning.set_line_wrap(True)
            grid.attach(warning, 0, row, 3, 1)
            row += 1
            self.apply_theme_check.set_sensitive(False)
            self.dark_theme_combo.set_sensitive(False)
            self.light_theme_combo.set_sensitive(False)

        self.gtksettings = Gtk.Settings.get_default()
        font_button = Gtk.FontButton(show_style=False)
        font_button.set_filter_func(self.font_filter, None)
        self.gtksettings.bind_property(
            "gtk-font-name",
            font_button,
            "font-name",
            BindingFlags.BIDIRECTIONAL | BindingFlags.SYNC_CREATE,
        )
        font_button.connect("font-set", self.font_changed)
        font_label = Gtk.Label(label=_("%s: ") % _("Font"))
        font_label.set_halign(Gtk.Align.START)
        grid.attach(font_label, 0, row, 1, 1)
        grid.attach(font_button, 1, row, 2, 1)
        row += 1

        if win():
            self.sc_text = Gtk.CheckButton.new_with_mnemonic(
                _("Fixed Scrollbar (requires restart)")
            )
            self.sc_text.set_active(_bool_from_config(KEY_FIXED_SCROLL, False))
            self.sc_text.connect("toggled", self.scroll_changed)
            grid.attach(self.sc_text, 0, row, 3, 1)
            row += 1

        apply_now = Gtk.Button(label=_("Apply now"), expand=False)
        apply_now.connect("clicked", self.apply_now_clicked)
        grid.attach(apply_now, 0, row, 1, 1)

        default_btn = Gtk.Button(label=_("Restore to defaults"), expand=False)
        default_btn.connect("clicked", self.default_clicked)
        grid.attach(default_btn, 1, row, 1, 1)

        self._sync_dark_toggle()

        return _("Theme"), grid

    def _combo_value(self, combo):
        active = combo.get_active_id()
        if active:
            return active
        child = combo.get_child()
        if child:
            return child.get_text().strip()
        return ""

    def _set_combo_value(self, combo, value):
        combo.set_active_id(value)
        if combo.get_active_id() is None:
            combo.get_child().set_text(value)

    def _sync_dark_toggle(self):
        mode = _get_mode()
        if mode == MODE_AUTO:
            self.dark.set_sensitive(False)
            self.dark.handler_block_by_func(self.dark_variant_changed)
            self.dark.set_active(_effective_dark_preference())
            self.dark.handler_unblock_by_func(self.dark_variant_changed)
            return

        self.dark.set_sensitive(True)
        self.dark.handler_block_by_func(self.dark_variant_changed)
        self.dark.set_active(mode == MODE_DARK)
        self.dark.handler_unblock_by_func(self.dark_variant_changed)

    def mode_changed(self, combo):
        mode = combo.get_active_id() or MODE_AUTO
        if mode not in VALID_MODES:
            mode = MODE_AUTO
        config.set(KEY_MODE, mode)
        apply_theme_settings()
        self._sync_dark_toggle()

    def theme_dark_changed(self, combo):
        value = self._combo_value(combo)
        if value:
            config.set(KEY_THEME_DARK, value)
            apply_theme_settings()

    def theme_light_changed(self, combo):
        value = self._combo_value(combo)
        if value:
            config.set(KEY_THEME_LIGHT, value)
            apply_theme_settings()

    def apply_theme_toggled(self, obj):
        config.set(KEY_APPLY_THEME, str(obj.get_active()))
        apply_theme_settings()

    def css_fixes_toggled(self, obj):
        config.set(KEY_APPLY_CSS_FIXES, str(obj.get_active()))
        apply_theme_settings()

    def dark_variant_changed(self, obj):
        mode = MODE_DARK if obj.get_active() else MODE_LIGHT
        config.set(KEY_MODE, mode)
        self.mode_combo.set_active_id(mode)
        apply_theme_settings()
        self._sync_dark_toggle()

    def apply_now_clicked(self, obj):
        apply_theme_settings()

    def scroll_changed(self, obj):
        value = obj.get_active()
        config.set(KEY_FIXED_SCROLL, str(value))
        self.gtksettings.set_property("gtk-primary-button-warps-slider", not value)

        if hasattr(MyPrefs, "provider"):
            Gtk.StyleContext.remove_provider_for_screen(
                Screen.get_default(), MyPrefs.provider
            )
        if value:
            MyPrefs.provider = Gtk.CssProvider()
            css = (
                "* { -GtkScrollbar-has-backward-stepper: 1; "
                "-GtkScrollbar-has-forward-stepper: 1; }"
            )
            MyPrefs.provider.load_from_data(css.encode("utf8"))
            Gtk.StyleContext.add_provider_for_screen(
                Screen.get_default(),
                MyPrefs.provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        try:
            if value:
                subprocess.check_output("setx GTK_OVERLAY_SCROLLING 0", shell=True)
            else:
                subprocess.check_output(
                    "reg delete HKCU\\Environment /v GTK_OVERLAY_SCROLLING /f",
                    shell=True,
                )
        except subprocess.CalledProcessError:
            print("Cannot set environment variable GTK_OVERLAY_SCROLLING")

    def font_changed(self, obj):
        font = obj.get_font()
        config.set(KEY_FONT, font)

    def font_filter(self, family, face, *obj):
        desc = face.describe()
        if (
            desc.get_style() == Pango.Style.NORMAL
            and desc.get_weight() == Pango.Weight.NORMAL
        ):
            return True
        return False

    def default_clicked(self, obj):
        default_theme = getattr(GrampsPreferences, "def_theme", DEFAULT_THEME_LIGHT)
        default_font = getattr(GrampsPreferences, "def_font", "")

        config.set(KEY_MODE, MODE_AUTO)
        config.set(KEY_THEME_DARK, DEFAULT_THEME_DARK)
        config.set(KEY_THEME_LIGHT, default_theme or DEFAULT_THEME_LIGHT)
        config.set(KEY_THEME_DARK_VARIANT, "")
        config.set(KEY_APPLY_THEME, "True")
        config.set(KEY_APPLY_CSS_FIXES, "True")
        config.set(KEY_FONT, "")

        if default_font:
            self.gtksettings.set_property("gtk-font-name", default_font)

        apply_theme_settings()

        self.mode_combo.set_active_id(MODE_AUTO)
        self._set_combo_value(self.dark_theme_combo, DEFAULT_THEME_DARK)
        self._set_combo_value(
            self.light_theme_combo, default_theme or DEFAULT_THEME_LIGHT
        )
        self.apply_theme_check.set_active(True)
        self.css_fixes_check.set_active(True)
        self._sync_dark_toggle()

        if not win():
            return

        self.sc_text.set_active(False)
        config.set(KEY_FIXED_SCROLL, "")
        self.gtksettings.set_property("gtk-primary-button-warps-slider", 1)
        if hasattr(MyPrefs, "provider"):
            Gtk.StyleContext.remove_provider_for_screen(
                Screen.get_default(), MyPrefs.provider
            )
        try:
            subprocess.check_output(
                "reg delete HKCU\\Environment /v GTK_OVERLAY_SCROLLING /f",
                shell=True,
            )
        except subprocess.CalledProcessError:
            pass
