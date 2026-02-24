"""
HearThis.at Plugin for Rhythmbox  —  v3.0
Funkcje: wyszukiwanie artysty/globalnie/gatunki, paginacja,
         filtr Popular/New, czas trwania, zakres dat,
         typ artysty (tracks/likes/reshares),
         sortowanie lokalne, filtr lokalny, kolejka odtwarzania.
"""

import gi
gi.require_version("RB", "3.0")
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import GObject, GLib, Gtk, Gio, Peas, RB, GdkPixbuf
import urllib.request
import urllib.parse
import json
import threading
import re

API_BASE  = "https://api-v2.hearthis.at"
PAGE_SIZE = 20

MODE_FEATURED = "featured"
MODE_ARTIST   = "artist"
MODE_SEARCH   = "search"
MODE_GENRE    = "genre"


# ── Pomocnicze ────────────────────────────────────────────────

def api_get(path, params=None):
    url = API_BASE + path
    if params:
        clean = {k: v for k, v in params.items() if v not in (None, "", "—", "wszystkie")}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Rhythmbox-HearThis/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[HearThis] API error {url}: {e}")
        return None


def fetch_pixbuf(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Rhythmbox-HearThis/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        return loader.get_pixbuf()
    except Exception as e:
        print(f"[HearThis] image error: {e}")
        return None


def strip_html(text):
    if not text:
        return ""
    text = text.replace("<br>", "\n").replace("<br/>", "\n")
    return re.sub(r"<[^>]+>", "", text).strip()


def fmt_dur(val):
    try:
        s = int(val)
        return f"{s // 60}:{s % 60:02d}"
    except Exception:
        return "?"


def tracks_from_response(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return data.get("data", [])
    return []


# ── Entry type ────────────────────────────────────────────────

class HearThisEntryType(RB.RhythmDBEntryType):
    __gtype_name__ = "HearThisEntryType"

    def __init__(self):
        RB.RhythmDBEntryType.__init__(self, name="hearthis-stream", save_to_disk=False)

    def can_sync_metadata(self, entry):
        return False

    def sync_metadata(self, entry, changes):
        pass


# ── Source ────────────────────────────────────────────────────

class HearThisSource(RB.Source):
    __gtype_name__ = "HearThisSource"

    # kolumny ListStore
    COL_TITLE  = 0
    COL_ARTIST = 1
    COL_DUR    = 2
    COL_URL    = 3
    COL_DURSEC = 4   # int — do sortowania

    def setup(self, db, shell, entry_type):
        self._db         = db
        self._shell      = shell
        self._entry_type = entry_type

        self._mode          = MODE_FEATURED
        self._current_page  = 1
        self._query_text    = ""
        self._current_genre = ""
        self._sort_col      = self.COL_TITLE
        self._sort_asc      = True

        self._build_ui()
        threading.Thread(target=self._bg_load_genres,   daemon=True).start()
        threading.Thread(target=self._bg_load_featured, daemon=True).start()

    # ─────────────────────────────────────────
    #  UI
    # ─────────────────────────────────────────

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # ── Panel filtrów (zwijany) ──────────
        self._filter_revealer = Gtk.Revealer()
        self._filter_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self._filter_revealer.set_reveal_child(True)

        filter_frame = Gtk.Frame()
        filter_frame.set_margin_start(6); filter_frame.set_margin_end(6)
        filter_frame.set_margin_top(4);   filter_frame.set_margin_bottom(0)

        filter_grid = Gtk.Grid()
        filter_grid.set_column_spacing(8)
        filter_grid.set_row_spacing(4)
        filter_grid.set_margin_start(6); filter_grid.set_margin_end(6)
        filter_grid.set_margin_top(6);   filter_grid.set_margin_bottom(6)

        # wiersz 0: wyszukiwarka
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Artysta lub słowo kluczowe…")
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("activate", lambda w: self._start_artist())
        filter_grid.attach(self._search_entry, 0, 0, 3, 1)

        btn_artist = Gtk.Button(label="Artysta")
        btn_artist.set_tooltip_text("Wczytaj wszystkie utwory artysty")
        btn_artist.connect("clicked", lambda w: self._start_artist())
        filter_grid.attach(btn_artist, 3, 0, 1, 1)

        btn_search = Gtk.Button(label="Szukaj")
        btn_search.set_tooltip_text("Szukaj po tytule / tagu")
        btn_search.connect("clicked", lambda w: self._start_search())
        filter_grid.attach(btn_search, 4, 0, 1, 1)

        # wiersz 1: gatunek + typ artysty + popular/new
        filter_grid.attach(Gtk.Label(label="Gatunek:"), 0, 1, 1, 1)
        self._genre_combo = Gtk.ComboBoxText()
        self._genre_combo.append_text("— wszystkie —")
        self._genre_combo.set_active(0)
        self._genre_combo.set_hexpand(True)
        filter_grid.attach(self._genre_combo, 1, 1, 1, 1)

        btn_genre = Gtk.Button(label="Wczytaj gatunek")
        btn_genre.connect("clicked", lambda w: self._start_genre())
        filter_grid.attach(btn_genre, 2, 1, 1, 1)

        filter_grid.attach(Gtk.Label(label="Typ artysty:"), 3, 1, 1, 1)
        self._artist_type_combo = Gtk.ComboBoxText()
        for t in ["tracks", "likes", "reshares"]:
            self._artist_type_combo.append_text(t)
        self._artist_type_combo.set_active(0)
        filter_grid.attach(self._artist_type_combo, 4, 1, 1, 1)

        # wiersz 2: popular/new + czas trwania
        filter_grid.attach(Gtk.Label(label="Sortuj feed:"), 0, 2, 1, 1)
        self._feed_type_combo = Gtk.ComboBoxText()
        for ft in ["— domyślne —", "popular", "new"]:
            self._feed_type_combo.append_text(ft)
        self._feed_type_combo.set_active(0)
        filter_grid.attach(self._feed_type_combo, 1, 2, 1, 1)

        filter_grid.attach(Gtk.Label(label="Czas (min ±5):"), 2, 2, 1, 1)
        self._dur_spin = Gtk.SpinButton()
        self._dur_spin.set_adjustment(Gtk.Adjustment(value=0, lower=0, upper=240, step_increment=5, page_increment=10))
        self._dur_spin.set_value(0)
        self._dur_spin.set_tooltip_text("0 = bez filtra")
        filter_grid.attach(self._dur_spin, 3, 2, 1, 1)

        # wiersz 3: zakres dat
        filter_grid.attach(Gtk.Label(label="Data od:"), 0, 3, 1, 1)
        self._date_from = Gtk.Entry()
        self._date_from.set_placeholder_text("YYYY-MM-DD")
        self._date_from.set_max_length(10)
        filter_grid.attach(self._date_from, 1, 3, 1, 1)

        filter_grid.attach(Gtk.Label(label="Data do:"), 2, 3, 1, 1)
        self._date_to = Gtk.Entry()
        self._date_to.set_placeholder_text("YYYY-MM-DD")
        self._date_to.set_max_length(10)
        filter_grid.attach(self._date_to, 3, 3, 1, 1)

        btn_feed = Gtk.Button(label="Wczytaj feed")
        btn_feed.set_tooltip_text("Załaduj feed z powyższymi filtrami")
        btn_feed.connect("clicked", lambda w: self._start_featured())
        filter_grid.attach(btn_feed, 4, 3, 1, 1)

        filter_frame.add(filter_grid)
        self._filter_revealer.add(filter_frame)

        # ── Pasek narzędziowy (zawsze widoczny) ──
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.set_margin_start(6); toolbar.set_margin_end(6)
        toolbar.set_margin_top(4);   toolbar.set_margin_bottom(2)

        btn_toggle = Gtk.ToggleButton(label="▲ Filtry")
        btn_toggle.set_active(True)
        btn_toggle.connect("toggled", self._on_toggle_filters)
        toolbar.pack_start(btn_toggle, False, False, 0)

        toolbar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)

        self._btn_prev = Gtk.Button(label="◀")
        self._btn_prev.set_sensitive(False)
        self._btn_prev.connect("clicked", lambda w: self._go_prev())
        toolbar.pack_start(self._btn_prev, False, False, 0)

        self._btn_next = Gtk.Button(label="▶")
        self._btn_next.set_sensitive(False)
        self._btn_next.connect("clicked", lambda w: self._go_next())
        toolbar.pack_start(self._btn_next, False, False, 0)

        self._page_lbl = Gtk.Label(label="")
        toolbar.pack_start(self._page_lbl, False, False, 4)

        toolbar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)

        # filtr lokalny (zawężanie listy)
        toolbar.pack_start(Gtk.Label(label="Filtruj listę:"), False, False, 0)
        self._local_filter = Gtk.SearchEntry()
        self._local_filter.set_placeholder_text("zawęź wyniki…")
        self._local_filter.set_size_request(160, -1)
        self._local_filter.connect("changed", self._on_local_filter_changed)
        toolbar.pack_start(self._local_filter, False, False, 0)

        toolbar.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 2)
        self._mode_lbl = Gtk.Label(label="")
        toolbar.pack_start(self._mode_lbl, True, True, 0)

        # ── Info artysty ─────────────────────
        self._info_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._info_box.set_margin_start(6); self._info_box.set_margin_end(6); self._info_box.set_margin_bottom(2)
        self._info_box.set_no_show_all(True)
        self._avatar_img = Gtk.Image()
        self._artist_lbl = Gtk.Label()
        self._artist_lbl.set_xalign(0.0)
        self._artist_lbl.set_line_wrap(True)
        self._artist_lbl.set_max_width_chars(100)
        self._info_box.pack_start(self._avatar_img, False, False, 0)
        self._info_box.pack_start(self._artist_lbl, True, True, 0)

        # ── Status ───────────────────────────
        self._status_lbl = Gtk.Label(label="Wczytuję polecane…")
        self._status_lbl.set_xalign(0.0)
        self._status_lbl.set_margin_start(6); self._status_lbl.set_margin_bottom(2)

        # ── Lista utworów ────────────────────
        # 0=tytuł, 1=artysta, 2=czas(str), 3=url, 4=czas(int)
        self._store = Gtk.ListStore(str, str, str, str, int)

        # Model filtrujący (lokalny filtr tekstowy)
        self._filter_model = self._store.filter_new()
        self._filter_model.set_visible_func(self._row_visible)

        # Model sortujący
        self._sort_model = Gtk.TreeModelSort(model=self._filter_model)

        tv = Gtk.TreeView(model=self._sort_model)
        tv.set_headers_visible(True)
        tv.set_hexpand(True)
        tv.set_vexpand(True)
        tv.set_headers_clickable(True)

        cols_def = [
            ("Tytuł",   self.COL_TITLE,  True),
            ("Artysta", self.COL_ARTIST, False),
            ("Czas",    self.COL_DURSEC, False),  # sortuj po int
        ]
        display_col = [self.COL_TITLE, self.COL_ARTIST, self.COL_DUR]

        for i, (header, sort_id, expand) in enumerate(cols_def):
            renderer = Gtk.CellRendererText()
            col = Gtk.TreeViewColumn(header, renderer, text=display_col[i])
            col.set_sort_column_id(sort_id)
            col.set_resizable(True)
            col.set_expand(expand)
            if not expand:
                col.set_min_width(80)
            tv.append_column(col)

        self._sort_model.set_sort_column_id(self.COL_TITLE, Gtk.SortType.ASCENDING)

        tv.connect("row-activated", self._on_row_activated)
        self._treeview = tv

        # Kontekstowe menu prawego przycisku
        tv.connect("button-press-event", self._on_button_press)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.add(tv)

        # ── Złożenie ─────────────────────────
        root.pack_start(self._filter_revealer, False, False, 0)
        root.pack_start(toolbar, False, False, 0)
        root.pack_start(self._info_box, False, False, 0)
        root.pack_start(self._status_lbl, False, False, 0)
        root.pack_start(scroll, True, True, 0)
        root.show_all()

        self.pack_start(root, True, True, 0)
        self.show_all()

    # ── Akcje ─────────────────────────────────────────────────

    def _on_toggle_filters(self, btn):
        revealed = btn.get_active()
        self._filter_revealer.set_reveal_child(revealed)
        btn.set_label("▲ Filtry" if revealed else "▼ Filtry")

    def _get_api_filters(self):
        """Zbiera wspólne parametry filtrów z UI."""
        params = {}
        dur = int(self._dur_spin.get_value())
        if dur > 0:
            params["duration"] = dur
        df = self._date_from.get_text().strip()
        dt = self._date_to.get_text().strip()
        if df:
            params["show_feed_start"] = df
        if dt:
            params["show_feed_end"] = dt
        return params

    def _start_featured(self):
        self._mode         = MODE_FEATURED
        self._current_page = 1
        self._hide_artist_info()
        self._fetch_current()

    def _start_artist(self):
        name = self._search_entry.get_text().strip()
        if not name:
            self._set_status("Wpisz nazwę artysty.")
            return
        self._mode         = MODE_ARTIST
        self._query_text   = name
        self._current_page = 1
        self._hide_artist_info()
        self._fetch_current()

    def _start_search(self):
        query = self._search_entry.get_text().strip()
        if not query:
            self._set_status("Wpisz frazę.")
            return
        self._mode         = MODE_SEARCH
        self._query_text   = query
        self._current_page = 1
        self._hide_artist_info()
        self._fetch_current()

    def _start_genre(self):
        genre = self._genre_combo.get_active_text()
        if not genre or genre.startswith("—"):
            self._set_status("Wybierz gatunek.")
            return
        self._mode          = MODE_GENRE
        self._current_genre = genre
        self._current_page  = 1
        self._hide_artist_info()
        self._fetch_current()

    def _go_prev(self):
        if self._current_page > 1:
            self._current_page -= 1
            self._fetch_current()

    def _go_next(self):
        self._current_page += 1
        self._fetch_current()

    # ── Lokalny filtr ─────────────────────────────────────────

    def _on_local_filter_changed(self, entry):
        self._filter_text = entry.get_text().strip().lower()
        self._filter_model.refilter()

    def _row_visible(self, model, it, data):
        text = getattr(self, "_filter_text", "")
        if not text:
            return True
        title  = (model.get_value(it, self.COL_TITLE)  or "").lower()
        artist = (model.get_value(it, self.COL_ARTIST) or "").lower()
        return text in title or text in artist

    # ── Dispatcher ────────────────────────────────────────────

    def _fetch_current(self):
        p = self._current_page
        filters = self._get_api_filters()

        if self._mode == MODE_FEATURED:
            ft = self._feed_type_combo.get_active_text() or ""
            if ft and not ft.startswith("—"):
                filters["type"] = ft
            label = f"Feed ({ft or 'domyślny'})  •  strona {p}"
            self._set_status(f"Wczytuję feed strona {p}…")
            threading.Thread(target=self._bg_load_featured, args=(p, filters), daemon=True).start()

        elif self._mode == MODE_ARTIST:
            atype = self._artist_type_combo.get_active_text() or "tracks"
            label = f"Artysta: {self._query_text} [{atype}]  •  strona {p}"
            self._set_status(f"Wczytuję artystę '{self._query_text}' [{atype}] strona {p}…")
            threading.Thread(target=self._bg_load_artist, args=(self._query_text, atype, p, filters), daemon=True).start()

        elif self._mode == MODE_SEARCH:
            label = f"Wyszukiwanie: {self._query_text}  •  strona {p}"
            self._set_status(f"Szukam '{self._query_text}' strona {p}…")
            threading.Thread(target=self._bg_search, args=(self._query_text, p, filters), daemon=True).start()

        elif self._mode == MODE_GENRE:
            label = f"Gatunek: {self._current_genre}  •  strona {p}"
            self._set_status(f"Wczytuję '{self._current_genre}' strona {p}…")
            threading.Thread(target=self._bg_load_genre, args=(self._current_genre, p, filters), daemon=True).start()
        else:
            return

        GLib.idle_add(self._mode_lbl.set_text, label)

    # ── Background workers ────────────────────────────────────

    def _bg_load_featured(self, page=1, filters=None):
        params = {"page": page, "count": PAGE_SIZE}
        if filters:
            params.update(filters)
        data = api_get("/feed/", params)
        tracks = tracks_from_response(data)
        GLib.idle_add(self._populate, tracks, f"Feed", page)

    def _bg_load_genres(self):
        data = api_get("/categories/")
        if data and isinstance(data, list):
            genres = [g["id"] for g in data if isinstance(g, dict) and "id" in g]
            GLib.idle_add(self._populate_genres, genres)

    def _bg_load_artist(self, username, atype, page, filters):
        if page == 1:
            info = api_get(f"/{username}/")
            if info and isinstance(info, dict):
                GLib.idle_add(
                    self._show_artist_info,
                    info.get("avatar_url", ""),
                    info.get("description", ""),
                    info.get("username", username),
                )
        params = {"type": atype, "page": page, "count": PAGE_SIZE}
        params.update(filters or {})
        data = api_get(f"/{username}/", params)
        tracks = tracks_from_response(data)
        if tracks:
            GLib.idle_add(self._populate, tracks, f"Artysta: {username} [{atype}]", page)
        else:
            GLib.idle_add(self._set_status, f"Brak wyników: '{username}' [{atype}] strona {page}.")

    def _bg_search(self, query, page, filters):
        params = {"t": query, "page": page, "count": PAGE_SIZE}
        params.update(filters or {})
        data = api_get("/search/", params)
        tracks = tracks_from_response(data)
        if tracks:
            GLib.idle_add(self._populate, tracks, f"Wyniki: {query}", page)
        else:
            GLib.idle_add(self._set_status, f"Brak wyników dla '{query}' strona {page}.")

    def _bg_load_genre(self, genre, page, filters):
        params = {"page": page, "count": PAGE_SIZE}
        params.update(filters or {})
        data = api_get(f"/categories/{genre}/", params)
        tracks = tracks_from_response(data)
        if tracks:
            GLib.idle_add(self._populate, tracks, f"Gatunek: {genre}", page)
        else:
            GLib.idle_add(self._set_status, f"Brak wyników: gatunek '{genre}' strona {page}.")

    # ── UI helpers ────────────────────────────────────────────

    def _populate(self, tracks, status, page):
        self._store.clear()
        count = 0
        for t in tracks:
            if not isinstance(t, dict):
                continue
            url = t.get("stream_url", "")
            if not url:
                continue
            title  = t.get("title") or "Bez tytułu"
            user   = t.get("user", {})
            artist = user.get("username", "") if isinstance(user, dict) else ""
            try:
                secs = int(t.get("duration", 0))
            except Exception:
                secs = 0
            dur_str = fmt_dur(secs)
            self._store.append([title, artist, dur_str, url, secs])
            count += 1

        self._page_lbl.set_text(f"Strona {page}" if page else "")
        self._btn_prev.set_sensitive(bool(page and page > 1))
        self._btn_next.set_sensitive(count == PAGE_SIZE)
        self._status_lbl.set_text(f"{status}  —  {count} utworów")

        # wyczyść lokalny filtr przy nowym załadowaniu
        self._local_filter.set_text("")

    def _populate_genres(self, genres):
        for g in genres:
            self._genre_combo.append_text(g)

    def _show_artist_info(self, avatar_url, description, username):
        self._info_box.set_no_show_all(False)
        desc = strip_html(description)
        if len(desc) > 300:
            desc = desc[:300] + "…"
        self._artist_lbl.set_text(f"@{username}  {desc}")
        self._info_box.show_all()
        if avatar_url:
            threading.Thread(target=self._bg_load_avatar, args=(avatar_url,), daemon=True).start()

    def _hide_artist_info(self):
        GLib.idle_add(self._info_box.hide)

    def _bg_load_avatar(self, url):
        p = fetch_pixbuf(url)
        if p:
            scaled = p.scale_simple(56, 56, GdkPixbuf.InterpType.BILINEAR)
            GLib.idle_add(self._avatar_img.set_from_pixbuf, scaled)

    def _set_status(self, text):
        GLib.idle_add(self._status_lbl.set_text, text)

    # ── Kontekstowe menu ──────────────────────────────────────

    def _on_button_press(self, treeview, event):
        if event.button == 3:  # prawy klik
            path_info = treeview.get_path_at_pos(int(event.x), int(event.y))
            if path_info:
                path = path_info[0]
                treeview.get_selection().select_path(path)
                self._show_context_menu(event, path)
            return True
        return False

    def _show_context_menu(self, event, path):
        menu = Gtk.Menu()

        item_play = Gtk.MenuItem(label="▶  Odtwórz")
        item_play.connect("activate", lambda w: self._activate_path(path))
        menu.append(item_play)

        item_queue = Gtk.MenuItem(label="➕  Dodaj do kolejki")
        item_queue.connect("activate", lambda w: self._add_path_to_queue(path))
        menu.append(item_queue)

        menu.show_all()
        menu.popup_at_pointer(event)

    def _activate_path(self, path):
        it = self._sort_model.get_iter(path)
        url    = self._sort_model.get_value(it, self.COL_URL)
        title  = self._sort_model.get_value(it, self.COL_TITLE)
        artist = self._sort_model.get_value(it, self.COL_ARTIST)
        if url:
            threading.Thread(target=self._resolve_and_play, args=(url, title, artist), daemon=True).start()

    def _add_path_to_queue(self, path):
        it = self._sort_model.get_iter(path)
        url    = self._sort_model.get_value(it, self.COL_URL)
        title  = self._sort_model.get_value(it, self.COL_TITLE)
        artist = self._sort_model.get_value(it, self.COL_ARTIST)
        if url:
            threading.Thread(target=self._resolve_and_queue, args=(url, title, artist), daemon=True).start()

    # ── Odtwarzanie ───────────────────────────────────────────

    def _on_row_activated(self, treeview, path, column):
        self._activate_path(path)

    def _resolve_and_play(self, stream_url, title, artist):
        play_url = self._resolve_url(stream_url)
        if play_url:
            GLib.idle_add(self._play_url, play_url, title, artist)

    def _resolve_and_queue(self, stream_url, title, artist):
        play_url = self._resolve_url(stream_url)
        if play_url:
            GLib.idle_add(self._queue_url, play_url, title, artist)

    def _resolve_url(self, stream_url):
        """
        Strategia: 1) plik audio  2) API hearthis.at  3) podążaj za przekierowaniami
                   4) parsuj HTML  5) użyj URL jako-jest (może grać, może nie)
        Subskrypcyjne: HTTP 402/403 -> pokaż komunikat.
        """
        AUDIO_EXT = re.compile(r"[.](mp3|ogg|aac|flac|wav)([?]|$)", re.I)
        print(f"[HearThis] Resolving: {stream_url}")

        # 1. Już plik audio
        if AUDIO_EXT.search(stream_url):
            return stream_url

        # 2. API hearthis.at — pobierz download_url / stream_url
        if "hearthis.app" in stream_url or "hearthis.at" in stream_url:
            clean = re.sub(r"/listen/.*$", "", stream_url).rstrip("/")
            parts = [p for p in clean.split("/") if p and "." not in p]
            if len(parts) >= 2:
                user, slug = parts[-2], parts[-1]
                detail = api_get(f"/{user}/{slug}/")
                if detail and isinstance(detail, dict):
                    for key in ("download_url", "stream_url"):
                        c = detail.get(key, "")
                        if not c or "/listen/" in c:
                            continue
                        if AUDIO_EXT.search(c):
                            print(f"[HearThis] API ({key}): {c}")
                            return c
                        if key == "download_url" and c.startswith("http"):
                            print(f"[HearThis] download_url: {c}")
                            return c

        # 3. Podążaj za WSZYSTKIMI przekierowaniami (Accept: audio/*)
        try:
            current = stream_url
            for hop in range(8):
                req = urllib.request.Request(current, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "audio/mpeg, audio/ogg, audio/*, */*",
                })
                try:
                    resp = urllib.request.urlopen(req, timeout=8)
                    final = resp.geturl()
                    ctype = resp.headers.get("Content-Type", "")
                    resp.close()
                    if "audio" in ctype or "octet-stream" in ctype:
                        print(f"[HearThis] Audio stream hop {hop}: {final} [{ctype}]")
                        return final
                    if AUDIO_EXT.search(final):
                        print(f"[HearThis] Audio URL hop {hop}: {final}")
                        return final
                    if final == current:
                        break
                    print(f"[HearThis] hop {hop}: -> {final}")
                    current = final
                except urllib.error.HTTPError as e:
                    loc = e.headers.get("Location", "")
                    if loc:
                        print(f"[HearThis] HTTP {e.code} -> {loc}")
                        current = loc
                    elif e.code in (402, 403):
                        GLib.idle_add(
                            self._set_status,
                            "⚠ Ten utwór wymaga subskrypcji hearthis.at"
                        )
                        return ""
                    else:
                        print(f"[HearThis] HTTP {e.code} — stop")
                        break
        except Exception as e:
            print(f"[HearThis] Redirect error: {e}")

        # 4. Parsuj HTML
        try:
            req = urllib.request.Request(
                stream_url,
                headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                html = resp.read(256 * 1024).decode("utf-8", errors="ignore")
            patterns = [
                r'<(?:audio|source)[^>]+src=["\'](.*?)["\']',
                r'"(?:contentUrl|stream_url|streamUrl|mp3)":\s*"([^"]+)"',
                r'(https?://[^\s"\'<>]+\.mp3(?:\?[^\s"\'<>]*)?)',
                r'data-(?:stream|src|audio|url)=["\'](.*?)["\']',
            ]
            for pat in patterns:
                m = re.search(pat, html, re.I)
                if m:
                    c = m.group(1)
                    if c.startswith("http"):
                        print(f"[HearThis] HTML: {c}")
                        return c
        except Exception as e:
            print(f"[HearThis] HTML error: {e}")

        print(f"[HearThis] Unresolved: {stream_url}")
        return stream_url

    def _ensure_entry(self, url, title, artist):
        if not url:
            return None
        db = self._db
        entry = db.entry_lookup_by_location(url)
        if entry is not None:
            return entry

        methods = [
            ("db.entry_new",           lambda: db.entry_new(self._entry_type, url)),
            ("RB.RhythmDB.entry_new",  lambda: RB.RhythmDB.entry_new(db, self._entry_type, url)),
            ("RB.RhythmDBEntry.new",   lambda: RB.RhythmDBEntry.new(db, self._entry_type, url)),
        ]
        for name, fn in methods:
            try:
                entry = fn()
                if entry is not None:
                    print(f"[HearThis] entry_new OK via {name}")
                    break
                print(f"[HearThis] {name} returned None")
            except Exception as e:
                print(f"[HearThis] {name}: {type(e).__name__}: {e}")
                entry = None

        if entry is None:
            print(f"[HearThis] BŁĄD: wszystkie entry_new zawiodły dla {url}")
            return None

        db.entry_set(entry, RB.RhythmDBPropType.TITLE,             title)
        db.entry_set(entry, RB.RhythmDBPropType.ARTIST,            artist)
        db.entry_set(entry, RB.RhythmDBPropType.STREAM_SONG_TITLE, title)
        db.commit()
        return entry

    def _play_url(self, url, title, artist):
        if not url:
            return
        print(f"[HearThis] Play: {url}")
        player = self._shell.props.shell_player
        entry = self._ensure_entry(url, title, artist)
        if entry:
            try:
                player.play_entry(entry, self)
                return
            except Exception as e:
                print(f"[HearThis] play_entry error: {e}")
        # Fallback bez wpisu DB
        try:
            player.load_uri(url, self, None)
        except Exception as e:
            print(f"[HearThis] load_uri error: {e}")
            self._set_status(f"Błąd odtwarzania: {e}")

    def _queue_url(self, url, title, artist):
        if not url:
            return
        print(f"[HearThis] Queue: {url}")
        entry = self._ensure_entry(url, title, artist)
        if entry:
            try:
                self._shell.props.shell_player.add_to_queue(entry)
                self._set_status(f"Dodano do kolejki: {title}")
            except Exception as e:
                print(f"[HearThis] add_to_queue error: {e}")

# ── Plugin ────────────────────────────────────────────────────

class HearThisPlugin(GObject.Object, Peas.Activatable):
    __gtype_name__ = "HearThisPlugin"
    object = GObject.property(type=GObject.Object)

    def __init__(self):
        GObject.Object.__init__(self)
        self._source     = None
        self._entry_type = None

    def do_activate(self):
        shell = self.object
        db    = shell.props.db

        self._entry_type = HearThisEntryType()
        db.register_entry_type(self._entry_type)

        self._source = GObject.new(
            HearThisSource,
            shell=shell,
            name="HearThis.at",
            entry_type=self._entry_type,
            icon=Gio.ThemedIcon.new("network-server-symbolic"),
        )
        self._source.setup(db, shell, self._entry_type)

        group = RB.DisplayPageGroup.get_by_id("library")
        shell.register_entry_type_for_source(self._source, self._entry_type)
        shell.append_display_page(self._source, group)
        print("[HearThis] Plugin activated.")

    def do_deactivate(self):
        if self._source:
            self._source.delete_thyself()
            self._source = None
        self._entry_type = None
        print("[HearThis] Plugin deactivated.")
