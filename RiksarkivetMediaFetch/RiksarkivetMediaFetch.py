#
# Gramps - a GTK+/GNOME based genealogy program
#
# Copyright (C) 2026  slashmad
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

"""Tools/Utilities/Riksarkivet citation media fetcher."""

# -------------------------------------------------------------------------
#
# Python modules
#
# -------------------------------------------------------------------------
import html as html_lib
import json
import logging
import os
import re
import shutil
import tempfile
from http.cookiejar import CookieJar
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

# -------------------------------------------------------------------------
#
# GTK modules
#
# -------------------------------------------------------------------------
from gi.repository import Gtk

# -------------------------------------------------------------------------
#
# Gramps modules
#
# -------------------------------------------------------------------------
from gramps.gen.const import GRAMPS_LOCALE as glocale
from gramps.gen.db import DbTxn
from gramps.gen.lib import Media, MediaRef
from gramps.gen.mime import get_type, is_valid_type
from gramps.gen.utils.file import create_checksum, media_path, media_path_full, relative_path
from gramps.gui.managedwindow import ManagedWindow
from gramps.gui.plug import tool
from gramps.gui.utils import ProgressMeter

try:
    import keyring
except Exception:
    keyring = None

try:
    _trans = glocale.get_addon_translator(__file__)
except ValueError:
    _trans = glocale.translation
_ = _trans.gettext

LOG = logging.getLogger(".riksarkivetfetch")

BILDVISNING_RE = re.compile(
    r"(?:https?://)?sok\.riksarkivet\.se/bildvisning/([A-Za-z0-9_]+)", re.IGNORECASE
)
HREF_RE = re.compile(r"href\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
RAW_HTTP_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
DIMENSION_RE = re.compile(r"(\d{3,5})\s*[xX]\s*(\d{3,5})")
MANIFEST_HINT_RE = re.compile(r"(manifest|iiif).+\.json", re.IGNORECASE)
LOGIN_FORM_RE = re.compile(
    r"<form[^>]*action=['\"]([^'\"]+)['\"][^>]*method=['\"]post['\"][^>]*>(.*?)</form>",
    re.IGNORECASE | re.DOTALL,
)
INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
INPUT_ATTR_RE = re.compile(r"([A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*['\"]([^'\"]*)['\"]")
KEYRING_SERVICE = "Gramps.RiksarkivetMediaFetch"
KEYRING_USER_KEY = "username"
KEYRING_PASS_KEY = "password"


class RiksarkivetFetchError(RuntimeError):
    """Raised when a citation cannot be fetched from Riksarkivet."""


class RiksarkivetMediaFetchTool(tool.Tool, ManagedWindow):
    """Batch-fetch citation media from Riksarkivet links."""

    def __init__(self, dbstate, user, options_class, name, callback=None):
        self.user = user
        self.dbstate = dbstate
        self.db = dbstate.db
        self.uistate = user.uistate
        self._citation_media_dir = None
        self._media_by_abs_path = {}
        self._media_by_checksum = {}
        self._cookie_jar = CookieJar()
        self._opener = build_opener(HTTPCookieProcessor(self._cookie_jar))
        self._auth_enabled = False
        self._auth_username = ""
        self._auth_password = ""
        self._auth_attempted = False
        self._auth_logged_in = False
        self._run_options = None

        ManagedWindow.__init__(self, self.uistate, [], self.__class__)
        self.set_window(Gtk.Window(), Gtk.Label(), "")
        tool.Tool.__init__(self, dbstate, options_class, name)

        dialog = self._build_dialog()
        response = dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            self._run_options = self._collect_dialog_options()
        dialog.destroy()

        if self._run_options:
            self._run_tool()

        self.close()

    def _build_dialog(self):
        dialog = Gtk.Dialog(
            _("Fetch Riksarkivet citation media"),
            self.uistate.window,
            Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
            (Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.ACCEPT),
        )

        content = dialog.get_content_area()
        content.set_spacing(8)

        intro = Gtk.Label(
            _(
                "Find citation links that point to Riksarkivet bildvisning pages, "
                "download the best available image, store it under media/citations, "
                "and attach it to each citation."
            )
        )
        intro.set_line_wrap(True)
        intro.set_xalign(0.0)
        content.pack_start(intro, False, False, 0)

        scope_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        scope_label = Gtk.Label(label=_("Scope:"))
        scope_label.set_xalign(0.0)
        self.scope_combo = Gtk.ComboBoxText()
        self.scope_combo.append("active", _("Active citation"))
        self.scope_combo.append("all_riks", _("All citations with Riksarkivet links"))
        self.scope_combo.append("all", _("All citations"))
        self.scope_combo.set_active_id("all_riks")
        scope_box.pack_start(scope_label, False, False, 0)
        scope_box.pack_start(self.scope_combo, True, True, 0)
        content.pack_start(scope_box, False, False, 0)

        self.only_missing_check = Gtk.CheckButton.new_with_label(
            _("Skip citations that already have media attached")
        )
        self.only_missing_check.set_active(True)
        content.pack_start(self.only_missing_check, False, False, 0)

        self.reuse_check = Gtk.CheckButton.new_with_label(
            _("Reuse existing media objects when checksum matches")
        )
        self.reuse_check.set_active(True)
        content.pack_start(self.reuse_check, False, False, 0)

        self.auth_check = Gtk.CheckButton.new_with_label(
            _("Use Riksarkivet login for protected pages")
        )
        self.auth_check.set_active(False)
        self.auth_check.connect("toggled", self._on_auth_toggled)
        content.pack_start(self.auth_check, False, False, 0)

        auth_grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        auth_user_label = Gtk.Label(label=_("Username:"))
        auth_user_label.set_xalign(0.0)
        self.auth_user_entry = Gtk.Entry()
        self.auth_user_entry.set_hexpand(True)
        auth_pass_label = Gtk.Label(label=_("Password:"))
        auth_pass_label.set_xalign(0.0)
        self.auth_pass_entry = Gtk.Entry()
        self.auth_pass_entry.set_visibility(False)
        self.auth_pass_entry.set_invisible_char("*")
        self.auth_pass_entry.set_hexpand(True)
        auth_grid.attach(auth_user_label, 0, 0, 1, 1)
        auth_grid.attach(self.auth_user_entry, 1, 0, 1, 1)
        auth_grid.attach(auth_pass_label, 0, 1, 1, 1)
        auth_grid.attach(self.auth_pass_entry, 1, 1, 1, 1)
        content.pack_start(auth_grid, False, False, 0)

        self.save_auth_check = Gtk.CheckButton.new_with_label(
            _("Save login in keyring (if available)")
        )
        self.save_auth_check.set_active(keyring is not None)
        self.save_auth_check.set_sensitive(keyring is not None)
        content.pack_start(self.save_auth_check, False, False, 0)

        saved_user, saved_pass = self._load_saved_login()
        if saved_user:
            self.auth_user_entry.set_text(saved_user)
        if saved_pass:
            self.auth_pass_entry.set_text(saved_pass)
        if saved_user or saved_pass:
            self.auth_check.set_active(True)

        self._on_auth_toggled(self.auth_check)

        dialog.show_all()
        return dialog

    def _collect_dialog_options(self):
        return {
            "scope": self.scope_combo.get_active_id() or "all_riks",
            "only_missing": self.only_missing_check.get_active(),
            "reuse_existing": self.reuse_check.get_active(),
            "auth_enabled": self.auth_check.get_active(),
            "auth_username": self.auth_user_entry.get_text(),
            "auth_password": self.auth_pass_entry.get_text(),
            "save_auth": self.save_auth_check.get_active() if keyring is not None else False,
        }

    def _show_message(self, title, text, message_type=Gtk.MessageType.INFO):
        dialog = Gtk.MessageDialog(
            transient_for=self.uistate.window,
            flags=Gtk.DialogFlags.MODAL | Gtk.DialogFlags.DESTROY_WITH_PARENT,
            message_type=message_type,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dialog.format_secondary_text(text)
        dialog.set_keep_above(True)
        dialog.present()
        dialog.run()
        dialog.destroy()

    def _on_auth_toggled(self, _widget):
        enabled = self.auth_check.get_active()
        self.auth_user_entry.set_sensitive(enabled)
        self.auth_pass_entry.set_sensitive(enabled)
        self.save_auth_check.set_sensitive(enabled and keyring is not None)

    def _run_tool(self):
        opts = self._run_options or {}
        scope = opts.get("scope", "all_riks")
        only_missing = bool(opts.get("only_missing", True))
        reuse_existing = bool(opts.get("reuse_existing", True))
        self._auth_enabled = bool(opts.get("auth_enabled", False))
        self._auth_username = str(opts.get("auth_username", "")).strip()
        self._auth_password = str(opts.get("auth_password", ""))
        save_auth = bool(opts.get("save_auth", False))
        self._auth_attempted = False
        self._auth_logged_in = False

        if self._auth_enabled and (not self._auth_username or not self._auth_password):
            saved_user, saved_pass = self._load_saved_login()
            if not self._auth_username:
                self._auth_username = saved_user
            if not self._auth_password:
                self._auth_password = saved_pass

        if self._auth_enabled and (not self._auth_username or not self._auth_password):
            self._show_message(
                _("Login details missing"),
                _("Enable login only when both username and password are provided."),
                message_type=Gtk.MessageType.WARNING,
            )
            return

        if self._auth_enabled and save_auth:
            self._save_login(self._auth_username, self._auth_password)

        citation_handles = self._collect_citation_handles(scope)
        if not citation_handles:
            self._show_message(
                _("Nothing to do"),
                _("No citations matched the selected scope."),
            )
            return

        self._build_media_index()
        stats = {
            "linked": 0,
            "skipped": 0,
            "error": 0,
        }
        errors = []
        aborted = False

        progress = ProgressMeter(
            _("Fetching Riksarkivet media"),
            "",
            can_cancel=True,
            parent=self.uistate.window,
        )
        progress.set_pass(_("Processing citations"), len(citation_handles))

        self.db.disable_signals()
        try:
            with DbTxn(_("Fetch Riksarkivet citation media"), self.db, batch=True) as trans:
                for citation_handle in citation_handles:
                    if progress.get_cancelled():
                        aborted = True
                        break

                    citation = self.db.get_citation_from_handle(citation_handle)
                    if citation is None:
                        stats["skipped"] += 1
                        progress.step()
                        continue

                    status, detail = self._process_citation(
                        citation,
                        trans,
                        only_missing=only_missing,
                        reuse_existing=reuse_existing,
                    )
                    stats[status] += 1
                    if status == "error" and detail:
                        errors.append(detail)
                    progress.step()
        finally:
            self.db.enable_signals()
            self.db.request_rebuild()
            progress.close()

        summary = _(
            "Completed.\n\n"
            "Linked: %(linked)d\n"
            "Skipped: %(skipped)d\n"
            "Errors: %(error)d"
        ) % stats
        if aborted:
            summary = _("Aborted by user.\n\n") + summary

        if errors:
            shown_errors = "\n".join(errors[:20])
            if len(errors) > 20:
                shown_errors += "\n..."
            self.user.info(
                _("Riksarkivet fetch completed"),
                summary + "\n\n" + shown_errors,
                parent=self.uistate.window,
                monospaced=True,
            )
        else:
            self._show_message(_("Riksarkivet fetch completed"), summary)

    def _load_saved_login(self):
        if keyring is None:
            return "", ""
        try:
            user = keyring.get_password(KEYRING_SERVICE, KEYRING_USER_KEY) or ""
            password = keyring.get_password(KEYRING_SERVICE, KEYRING_PASS_KEY) or ""
            return user, password
        except Exception as err:
            LOG.debug("Unable to read keyring credentials: %s", err)
            return "", ""

    def _save_login(self, username, password):
        if keyring is None:
            return
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER_KEY, username)
            keyring.set_password(KEYRING_SERVICE, KEYRING_PASS_KEY, password)
        except Exception as err:
            LOG.debug("Unable to save keyring credentials: %s", err)

    def _collect_citation_handles(self, scope):
        if scope == "active":
            active = self.uistate.get_active("Citation")
            return [active] if active else []

        all_handles = self.db.get_citation_handles(sort_handles=False)
        if scope == "all":
            return all_handles

        result = []
        for handle in all_handles:
            citation = self.db.get_citation_from_handle(handle)
            if citation and self._extract_riksarkivet_url(citation.get_page() or ""):
                result.append(handle)
        return result

    def _process_citation(self, citation, trans, only_missing, reuse_existing):
        citation_id = citation.get_gramps_id() or citation.get_handle()

        if only_missing and citation.get_media_list():
            return "skipped", None

        source_url = self._extract_riksarkivet_url(citation.get_page() or "")
        if not source_url:
            return "skipped", None

        temp_path = None
        try:
            image_url, bildid = self._resolve_best_image_url(source_url)
            temp_path = self._download_to_temp(image_url, bildid, source_url=source_url)
            checksum = create_checksum(temp_path)

            if reuse_existing and checksum:
                existing_handle = self._media_by_checksum.get(checksum)
                if existing_handle:
                    linked = self._link_media_to_citation(citation, existing_handle, trans)
                    return ("linked" if linked else "skipped"), None

            final_abs = self._store_download(temp_path, bildid, checksum)
            temp_path = None

            media_obj = self._find_media_by_abs_path(final_abs)
            if media_obj is None:
                media_obj = self._create_media_object(final_abs, checksum, trans)
            elif checksum and not media_obj.get_checksum():
                media_obj.set_checksum(checksum)
                self.db.commit_media(media_obj, trans)
                self._index_media_object(media_obj)

            linked = self._link_media_to_citation(citation, media_obj.get_handle(), trans)
            return ("linked" if linked else "skipped"), None
        except Exception as err:
            LOG.exception("Riksarkivet fetch failed for citation %s", citation_id)
            return "error", "%s: %s" % (citation_id, err)
        finally:
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)

    def _extract_riksarkivet_url(self, text):
        match = BILDVISNING_RE.search(text or "")
        if not match:
            return None
        url = match.group(0)
        if not url.lower().startswith("http"):
            url = "https://" + url.lstrip("/")
        return url

    def _resolve_best_image_url(self, source_url):
        raw, content_type, final_url = self._http_get(
            source_url,
            accept="text/html,*/*",
            referer=source_url,
        )

        if content_type.startswith("image/"):
            return final_url, self._extract_bildid(source_url)

        html_text = raw.decode("utf-8", errors="replace")
        if self._looks_like_login_page(html_text):
            if not self._auth_enabled:
                raise RiksarkivetFetchError(
                    _(
                        "This Riksarkivet page requires login. "
                        "Enable login and provide username/password."
                    )
                )
            self._login_riksarkivet(source_url, html_text)
            raw, content_type, final_url = self._http_get(
                source_url,
                accept="text/html,*/*",
                referer=source_url,
            )
            if content_type.startswith("image/"):
                return final_url, self._extract_bildid(source_url)
            html_text = raw.decode("utf-8", errors="replace")
            if self._looks_like_login_page(html_text):
                raise RiksarkivetFetchError(_("Login failed for this Riksarkivet page."))

        candidates = self._extract_image_candidates_from_html(html_text, final_url)
        if candidates:
            return candidates[0], self._extract_bildid(source_url)

        manifest_urls = self._extract_manifest_urls(html_text, final_url)
        for manifest_url in manifest_urls:
            image_url = self._resolve_image_from_manifest(manifest_url, source_url=source_url)
            if image_url:
                return image_url, self._extract_bildid(source_url)

        bildid = self._extract_bildid(source_url)
        if bildid:
            for guessed in self._guess_iiif_urls(bildid):
                if self._probe_image_url(guessed, source_url=source_url):
                    return guessed, bildid

        raise RiksarkivetFetchError(_("Could not find a downloadable image URL."))

    def _looks_like_login_page(self, html_text):
        lowered = (html_text or "").lower()
        if "action=\"/login\"" in lowered and "name=\"password\"" in lowered:
            return True
        if "<title>logga in" in lowered or "<title>login" in lowered:
            return True
        return False

    def _login_riksarkivet(self, source_url, html_text):
        if self._auth_logged_in:
            return
        if self._auth_attempted and not self._auth_logged_in:
            raise RiksarkivetFetchError(_("Riksarkivet login failed."))

        self._auth_attempted = True
        action, fields = self._extract_login_form(html_text)
        if not action:
            raise RiksarkivetFetchError(_("Could not find Riksarkivet login form."))

        fields["Username"] = self._auth_username
        fields["Password"] = self._auth_password
        fields["SaveLogin"] = "false"
        fields.setdefault("Url", urlparse(source_url).path or "/")
        fields.setdefault("ReturnUrlIsEncoded", "False")
        fields.setdefault("loginKnappen", "vanlig")

        payload = urlencode(fields).encode("utf-8")
        login_url = urljoin(source_url, action)
        request = Request(
            login_url,
            data=payload,
            headers={
                "User-Agent": "Gramps-RiksarkivetFetch/0.1",
                "Accept": "text/html,*/*",
                "Referer": source_url,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )

        try:
            with self._opener.open(request, timeout=45) as response:
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as err:
            raise RiksarkivetFetchError(_("Login HTTP error: %s (%s)") % (err.reason, err.code))
        except URLError as err:
            raise RiksarkivetFetchError(_("Login network error: %s") % err.reason)

        if self._looks_like_login_page(body):
            raise RiksarkivetFetchError(_("Invalid login credentials for Riksarkivet."))

        self._auth_logged_in = True

    def _extract_login_form(self, html_text):
        match = LOGIN_FORM_RE.search(html_text or "")
        if not match:
            return None, {}

        action = html_lib.unescape(match.group(1).strip())
        form_body = match.group(2)
        fields = {}

        for input_match in INPUT_TAG_RE.finditer(form_body):
            tag = input_match.group(0)
            attrs = {}
            for attr_match in INPUT_ATTR_RE.finditer(tag):
                attrs[attr_match.group(1).lower()] = html_lib.unescape(attr_match.group(2))
            name = attrs.get("name")
            if not name:
                continue
            input_type = attrs.get("type", "").lower()
            if input_type == "checkbox":
                # Keep explicit hidden value if present; do not force checkbox true.
                if name not in fields:
                    fields[name] = attrs.get("value", "on")
                continue
            fields[name] = attrs.get("value", "")

        return action, fields

    def _extract_bildid(self, source_url):
        match = BILDVISNING_RE.search(source_url or "")
        return match.group(1) if match else "unknown"

    def _extract_image_candidates_from_html(self, html_text, base_url):
        scored = {}

        for match in HREF_RE.finditer(html_text):
            href = html_lib.unescape(match.group(1).strip())
            absolute = urljoin(base_url, href)
            if not self._looks_like_image_url(absolute):
                continue
            context = html_text[max(0, match.start() - 180) : min(len(html_text), match.end() + 180)]
            score = self._score_candidate(absolute, context)
            scored[absolute] = max(score, scored.get(absolute, 0))

        for raw_url in RAW_HTTP_URL_RE.findall(html_text):
            absolute = html_lib.unescape(raw_url.strip())
            if not self._looks_like_image_url(absolute):
                continue
            score = self._score_candidate(absolute, absolute)
            scored[absolute] = max(score, scored.get(absolute, 0))

        ranked = sorted(scored.items(), key=lambda item: item[1], reverse=True)
        return [url for url, _score in ranked]

    def _extract_manifest_urls(self, html_text, base_url):
        urls = set()

        for raw_url in RAW_HTTP_URL_RE.findall(html_text):
            absolute = html_lib.unescape(raw_url.strip())
            if MANIFEST_HINT_RE.search(absolute):
                urls.add(absolute)

        for key_match in re.finditer(
            r'"(?:manifest|iiif)[^"\\]*"\s*:\s*"([^"\\]+)"', html_text, re.IGNORECASE
        ):
            maybe_url = html_lib.unescape(key_match.group(1).strip())
            urls.add(urljoin(base_url, maybe_url))

        return sorted(urls)

    def _resolve_image_from_manifest(self, manifest_url, source_url=None):
        try:
            raw, _content_type, _final_url = self._http_get(
                manifest_url,
                accept="application/json,*/*",
                referer=source_url or manifest_url,
            )
            manifest = json.loads(raw.decode("utf-8", errors="replace"))
        except Exception as err:
            LOG.debug("Unable to parse manifest %s: %s", manifest_url, err)
            return None

        candidates = set()
        self._collect_image_candidates_from_manifest(manifest, candidates)
        if not candidates:
            return None

        ranked = sorted(candidates, key=self._score_image_url, reverse=True)
        return ranked[0]

    def _collect_image_candidates_from_manifest(self, node, out_urls):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("id", "@id") and isinstance(value, str):
                    if self._looks_like_image_url(value):
                        out_urls.add(value)

                if key in ("service", "services"):
                    self._collect_service_candidates(value, out_urls)

                self._collect_image_candidates_from_manifest(value, out_urls)
        elif isinstance(node, list):
            for item in node:
                self._collect_image_candidates_from_manifest(item, out_urls)

    def _collect_service_candidates(self, service_node, out_urls):
        if isinstance(service_node, dict):
            service_id = service_node.get("id") or service_node.get("@id")
            if isinstance(service_id, str):
                service_id = service_id.rstrip("/")
                out_urls.add(service_id + "/full/max/0/default.jpg")
                out_urls.add(service_id + "/full/full/0/default.jpg")

            if "service" in service_node:
                self._collect_service_candidates(service_node["service"], out_urls)
        elif isinstance(service_node, list):
            for item in service_node:
                self._collect_service_candidates(item, out_urls)

    def _guess_iiif_urls(self, bildid):
        return [
            "https://lbiiif.riksarkivet.se/arkis!%s/full/max/0/default.jpg" % bildid,
            "https://lbiiif.riksarkivet.se/arkis!%s/full/full/0/default.jpg" % bildid,
            "https://sok.riksarkivet.se/iiif/arkis!%s/full/max/0/default.jpg" % bildid,
            "https://sok.riksarkivet.se/iiif/arkis!%s/full/full/0/default.jpg" % bildid,
            "https://iiif.riksarkivet.se/iiif/arkis!%s/full/max/0/default.jpg" % bildid,
            "https://iiif.riksarkivet.se/iiif/arkis!%s/full/full/0/default.jpg" % bildid,
        ]

    def _probe_image_url(self, image_url, source_url=None):
        try:
            _raw, content_type, _final_url = self._http_get(
                image_url,
                accept="image/*,*/*",
                referer=source_url,
            )
            return content_type.startswith("image/")
        except Exception:
            return False

    def _http_get(self, url, accept="*/*", referer=None):
        headers = {
            "User-Agent": "Gramps-RiksarkivetFetch/0.1",
            "Accept": accept,
        }
        if referer:
            headers["Referer"] = referer

        request = Request(
            url,
            headers=headers,
        )

        try:
            with self._opener.open(request, timeout=45) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "").lower()
                final_url = response.geturl()
                return payload, content_type, final_url
        except HTTPError as err:
            raise RiksarkivetFetchError("%s (%s)" % (err.reason, err.code))
        except URLError as err:
            raise RiksarkivetFetchError(str(err.reason))

    def _download_to_temp(self, image_url, bildid, source_url=None):
        suffix = self._guess_suffix(image_url)
        fd, temp_path = tempfile.mkstemp(prefix="gramps-riksarkivet-", suffix=suffix)
        os.close(fd)

        headers = {
            "User-Agent": "Gramps-RiksarkivetFetch/0.1",
            "Accept": "image/*,*/*",
        }
        if source_url:
            headers["Referer"] = source_url

        request = Request(
            image_url,
            headers=headers,
        )

        try:
            with self._opener.open(request, timeout=120) as response:
                content_type = response.headers.get("Content-Type", "").lower()
                with open(temp_path, "wb") as out_file:
                    shutil.copyfileobj(response, out_file)

            mime = get_type(temp_path)
            if not (content_type.startswith("image/") or is_valid_type(mime)):
                raise RiksarkivetFetchError(_("Download did not return a valid media file."))
            return temp_path
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    def _store_download(self, temp_path, bildid, checksum):
        destination_dir = self._get_citation_media_dir()
        base_name = self._safe_basename(bildid)
        extension = os.path.splitext(temp_path)[1] or ".jpg"

        target_name = base_name + extension
        target_path = os.path.join(destination_dir, target_name)

        if os.path.exists(target_path):
            existing_checksum = create_checksum(target_path)
            if checksum and existing_checksum and checksum == existing_checksum:
                os.remove(temp_path)
                return target_path
            target_path = os.path.join(
                destination_dir,
                self._next_available_filename(destination_dir, target_name),
            )

        shutil.move(temp_path, target_path)
        return target_path

    def _get_citation_media_dir(self):
        if self._citation_media_dir:
            return self._citation_media_dir

        base_dir = str(media_path(self.db))
        if not base_dir:
            raise RiksarkivetFetchError(_("No media base path is configured."))

        os.makedirs(base_dir, exist_ok=True)
        citation_dir = os.path.join(base_dir, "citations")
        os.makedirs(citation_dir, exist_ok=True)

        self._citation_media_dir = citation_dir
        return citation_dir

    def _safe_basename(self, value):
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "riksarkivet")
        cleaned = cleaned.strip("._")
        return cleaned or "riksarkivet"

    def _next_available_filename(self, directory, filename):
        root, ext = os.path.splitext(filename)
        candidate = filename
        index = 1
        while os.path.exists(os.path.join(directory, candidate)):
            candidate = "%s_%d%s" % (root, index, ext)
            index += 1
        return candidate

    def _build_media_index(self):
        self._media_by_abs_path = {}
        self._media_by_checksum = {}

        for media_obj in self.db.iter_media():
            self._index_media_object(media_obj)

    def _index_media_object(self, media_obj):
        abs_path = os.path.abspath(media_path_full(self.db, media_obj.get_path()))
        self._media_by_abs_path[abs_path] = media_obj

        checksum = media_obj.get_checksum()
        if checksum:
            self._media_by_checksum.setdefault(checksum, media_obj.get_handle())

    def _find_media_by_abs_path(self, abs_path):
        return self._media_by_abs_path.get(os.path.abspath(abs_path))

    def _create_media_object(self, abs_path, checksum, trans):
        base_dir = str(media_path(self.db))
        rel_path = relative_path(abs_path, base_dir)

        media_obj = Media()
        media_obj.set_path(rel_path)
        media_obj.set_mime_type(get_type(abs_path))
        media_obj.set_description(os.path.splitext(os.path.basename(abs_path))[0])
        if checksum:
            media_obj.set_checksum(checksum)

        self.db.add_media(media_obj, trans)
        self._index_media_object(media_obj)
        return media_obj

    def _link_media_to_citation(self, citation, media_handle, trans):
        for media_ref in citation.get_media_list():
            if media_ref.get_reference_handle() == media_handle:
                return False

        new_ref = MediaRef()
        new_ref.set_reference_handle(media_handle)
        citation.add_media_reference(new_ref)
        self.db.commit_citation(citation, trans)
        return True

    def _looks_like_image_url(self, url):
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False

        path = parsed.path.lower()
        if path.endswith((".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".jp2")):
            return True

        if "/iiif/" in path and "/full/" in path:
            return True

        return False

    def _score_candidate(self, url, context):
        score = self._score_image_url(url)

        dimension_match = DIMENSION_RE.search(context or "")
        if dimension_match:
            width = int(dimension_match.group(1))
            height = int(dimension_match.group(2))
            score += width * height

        lowered = url.lower()
        if "hela" in (context or "").lower():
            score += 50_000_000
        if "aktuell vy" in (context or "").lower():
            score -= 10_000_000
        if "1000x" in lowered:
            score -= 100_000_000

        return score

    def _score_image_url(self, url):
        lowered = url.lower()
        score = 0

        if "/full/max/" in lowered:
            score += 1_000_000_000
        elif "/full/full/" in lowered:
            score += 900_000_000

        if lowered.endswith(".jp2"):
            score += 300_000_000
        elif lowered.endswith((".jpg", ".jpeg")):
            score += 200_000_000
        elif lowered.endswith((".png", ".tif", ".tiff")):
            score += 100_000_000

        for dimension_match in DIMENSION_RE.finditer(lowered):
            width = int(dimension_match.group(1))
            height = int(dimension_match.group(2))
            score += width * height

        return score

    def _guess_suffix(self, image_url):
        path = urlparse(image_url).path.lower()
        for ext in (".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".jp2"):
            if path.endswith(ext):
                return ext
        return ".jpg"


class RiksarkivetMediaFetchOptions(tool.ToolOptions):
    """No persistent options are currently used for this tool."""

    def __init__(self, name, person_id=None):
        tool.ToolOptions.__init__(self, name, person_id)
