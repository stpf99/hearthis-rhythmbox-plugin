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
from collections import deque
import os
import shutil
import tempfile

API_BASE     = "https://api-v2.hearthis.at"
PAGE_SIZE    = 20
CACHE_DIR    = os.path.join(GLib.get_user_cache_dir(), "rhythmbox", "hearthis")
DOWNLOAD_DIR = os.path.join(GLib.get_home_dir(), "Muzyka", "HearThis.at")

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
    COL_TITLE    = 0
    COL_ARTIST   = 1
    COL_DUR      = 2
    COL_URL      = 3
    COL_DURSEC   = 4   # int — do sortowania
    COL_COVER    = 5   # str URL okładki
    COL_USERNAME = 6   # str username (do "przejdź do artysty")
    COL_PIXBUF   = 7   # GdkPixbuf — miniaturka okładki
    COL_DLURL    = 8   # str download_url (puste = brak)
    COL_DLSTATE  = 9   # str: ""|"yes"|"cached"|"local"

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

        # Historia nawigacji — deque krotek (mode, query, genre, page, label)
        self._history      = deque(maxlen=50)
        self._history_pos  = -1   # aktualny indeks w historii
        self._history_jump = False  # True gdy nawigujemy po historii (nie dodajemy)

        self._build_ui()

        player = shell.props.shell_player
        player.connect("elapsed-nano-changed", self._on_elapsed_changed)
        player.connect("playing-song-changed", self._on_playing_song_changed)
        self._pending_dur_entry = None   # wpis czekający na potwierdzenie dur

        # RB.ExtDB("album-art") — to samo co używa plugin cover art
        try:
            self._art_store = RB.ExtDB(name="album-art")
            print("[HearThis] ExtDB art store OK")
        except Exception as e:
            self._art_store = None
            print(f"[HearThis] ExtDB unavailable: {e}")

        # słownik url_stream → cover_url + (local_uri → cover_url)
        self._cover_map = {}

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

        # ── Historia nawigacji ──
        self._btn_back = Gtk.Button(label="⬅")
        self._btn_back.set_tooltip_text("Wróć (historia)")
        self._btn_back.set_sensitive(False)
        self._btn_back.connect("clicked", lambda w: self._history_back())
        toolbar.pack_start(self._btn_back, False, False, 0)

        self._btn_fwd = Gtk.Button(label="➡")
        self._btn_fwd.set_tooltip_text("Naprzód (historia)")
        self._btn_fwd.set_sensitive(False)
        self._btn_fwd.connect("clicked", lambda w: self._history_forward())
        toolbar.pack_start(self._btn_fwd, False, False, 0)

        self._hist_lbl = Gtk.Label(label="")
        self._hist_lbl.set_ellipsize(3)   # PANGO_ELLIPSIZE_END
        self._hist_lbl.set_max_width_chars(22)
        self._hist_lbl.set_tooltip_text("Aktualny widok")
        toolbar.pack_start(self._hist_lbl, False, False, 2)

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

        # Pre-download toggle
        self._predown_btn = Gtk.ToggleButton(label="📥 Pre-buf")
        self._predown_btn.set_tooltip_text(
            "Włączone: przed odtworzeniem pobierz utwór do cache — umożliwia przewijanie")
        self._predown_btn.set_active(False)
        toolbar.pack_start(self._predown_btn, False, False, 0)

        # Pobierz wszystkie widoczne (z download_url) do ~/Muzyka/HearThis.at
        self._btn_dl_all = Gtk.Button(label="⬇ Pobierz widok")
        self._btn_dl_all.set_tooltip_text("Pobierz wszystkie dostępne utwory z aktualnego widoku")
        self._btn_dl_all.connect("clicked", lambda w: self._download_all_visible())
        toolbar.pack_start(self._btn_dl_all, False, False, 0)

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
        self._store = Gtk.ListStore(str, str, str, str, int, str, str, GdkPixbuf.Pixbuf, str, str)

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

        # Kolumna miniaturki (40×40 px)
        rend_pix = Gtk.CellRendererPixbuf()
        rend_pix.set_property("width", 44)
        col_thumb = Gtk.TreeViewColumn("", rend_pix, pixbuf=self.COL_PIXBUF)
        col_thumb.set_fixed_width(44)
        col_thumb.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        tv.append_column(col_thumb)
        self._col_thumb_index = 0  # pozycja kolumny miniaturki

        cols_def = [
            ("Tytuł",   self.COL_TITLE,  True),
            ("Artysta", self.COL_ARTIST, False),
            ("Czas",    self.COL_DURSEC, False),
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

        # Kolumna "→ Artysta" — klikalna, wczytuje konto uploadera
        rend_link = Gtk.CellRendererText()
        rend_link.set_property("foreground", "#3584e4")
        rend_link.set_property("underline", 1)  # SINGLE
        col_go = Gtk.TreeViewColumn("→ Profil", rend_link, text=self.COL_USERNAME)
        col_go.set_resizable(True)
        col_go.set_min_width(90)
        tv.append_column(col_go)

        # Kolumna pobierania — ikona stanu + kliknięcie
        rend_dl = Gtk.CellRendererText()
        rend_dl.set_property("width", 32)
        col_dl = Gtk.TreeViewColumn("⬇", rend_dl)
        col_dl.set_cell_data_func(rend_dl, self._dl_cell_data_func)
        col_dl.set_fixed_width(36)
        col_dl.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        tv.append_column(col_dl)
        self._col_dl_index = len(cols_def) + 2  # po miniaturce i → Profil

        self._sort_model.set_sort_column_id(self.COL_TITLE, Gtk.SortType.ASCENDING)

        tv.connect("row-activated", self._on_row_activated)
        tv.connect("button-release-event", self._on_artist_link_click)
        tv.connect("button-press-event",   self._on_button_press)
        self._treeview = tv
        self._col_go_index = len(cols_def) + 1  # +1 bo miniaturka na początku


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

    def _on_elapsed_changed(self, player, elapsed_ns):
        """
        Gdy GStreamer zaraportuje postęp (elapsed_ns > 0), możemy odczytać
        rzeczywisty czas trwania i zapisać do DB — to umożliwia seek slider.
        """
        if not self._pending_dur_entry:
            return
        try:
            # get_playing_length zwraca długość w nanosekundach
            dur_ns = player.get_playing_length()
            if dur_ns and dur_ns > 0:
                dur_sec = int(dur_ns / 1e9)
                entry   = self._pending_dur_entry
                db      = self._db
                cur     = db.entry_get(entry, RB.RhythmDBPropType.DURATION)
                if not cur or cur != dur_sec:
                    db.entry_set(entry, RB.RhythmDBPropType.DURATION, dur_sec)
                    db.commit()
                    print(f"[HearThis] Duration confirmed by GStreamer: {dur_sec}s")
                self._pending_dur_entry = None
        except Exception as e:
            pass  # get_playing_length może nie istnieć w tej wersji RB

    def _on_playing_song_changed(self, player, entry):
        """Gdy zmienia się grający utwór — wyślij okładkę do RB art store."""
        if entry is None or self._art_store is None:
            return
        try:
            location  = entry.get_string(RB.RhythmDBPropType.LOCATION)
            cover_url = self._cover_map.get(location, "")

            artist = entry.get_string(RB.RhythmDBPropType.ARTIST)
            title  = entry.get_string(RB.RhythmDBPropType.TITLE)
            album  = entry.get_string(RB.RhythmDBPropType.ALBUM) or "hearthis.at"

            key = RB.ExtDBKey.create_storage("album", album)
            key.add_field("artist", artist)

            # Szukaj pixbuf w store (miniaturka już pobrana)
            pixbuf = self._find_pixbuf_for_url(location)

            if pixbuf:
                # Mamy pixbuf z miniaturki — użyj bezpośrednio (skaluj do 256)
                threading.Thread(
                    target=self._bg_push_art_pixbuf,
                    args=(key, pixbuf, cover_url),
                    daemon=True
                ).start()
            elif cover_url:
                # Brak miniaturki — pobierz z URL
                threading.Thread(
                    target=self._bg_push_art,
                    args=(key, cover_url),
                    daemon=True
                ).start()
        except Exception as e:
            print(f"[HearThis] art store error: {e}")

    def _find_pixbuf_for_url(self, stream_url):
        """Znajdź pixbuf w store dla danego URL streamu."""
        for i in range(len(self._store)):
            try:
                path = Gtk.TreePath.new_from_indices([i])
                it   = self._store.get_iter(path)
                if self._store.get_value(it, self.COL_URL) == stream_url:
                    return self._store.get_value(it, self.COL_PIXBUF)
            except Exception:
                pass
        return None

    def _bg_push_art_pixbuf(self, key, small_pixbuf, fallback_url):
        """Skaluj miniaturkę do 256px i wstaw do art store."""
        if not self._art_store:
            return
        try:
            # Najpierw spróbuj pobrać pełny rozmiar z URL
            if fallback_url:
                p = fetch_pixbuf(fallback_url)
                if p:
                    # Skaluj do 256×256 zachowując proporcje
                    w, h = p.get_width(), p.get_height()
                    scale = 256 / max(w, h, 1)
                    nw, nh = max(1, int(w*scale)), max(1, int(h*scale))
                    big = p.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
                    for m in ("store", "store_pixbuf"):
                        fn = getattr(self._art_store, m, None)
                        if fn:
                            fn(key, RB.ExtDBSourceType.SEARCH, big, None)
                            print(f"[HearThis] Art stored (full) via {m}: {nw}x{nh}")
                            return
            # Fallback: użyj miniaturki z store
            for m in ("store", "store_pixbuf"):
                fn = getattr(self._art_store, m, None)
                if fn:
                    fn(key, RB.ExtDBSourceType.SEARCH, small_pixbuf, None)
                    print(f"[HearThis] Art stored (thumb) via {m}")
                    return
        except Exception as e:
            print(f"[HearThis] push_art_pixbuf error: {e}")

    def _bg_push_art(self, key, cover_url):
        """Pobierz okładkę z URL i zapisz pixbuf do RB art store."""
        p = fetch_pixbuf(cover_url)
        if p and self._art_store:
            w, h = p.get_width(), p.get_height()
            scale = 256 / max(w, h, 1)
            nw, nh = max(1, int(w*scale)), max(1, int(h*scale))
            big = p.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
            for m in ("store", "store_pixbuf"):
                fn = getattr(self._art_store, m, None)
                if fn:
                    try:
                        fn(key, RB.ExtDBSourceType.SEARCH, big, None)
                        print(f"[HearThis] Art stored via {m}")
                        return
                    except Exception as e:
                        print(f"[HearThis] {m} error: {e}")

    def _push_history(self, label):
        """Zapisz aktualny stan do historii (chyba że nawigujemy po historii)."""
        if self._history_jump:
            return
        state = (self._mode, self._query_text, self._current_genre,
                 self._current_page, label)
        # Jeśli cofnęliśmy się i teraz robimy nowe wyszukiwanie — utnij "przyszłość"
        if self._history_pos < len(self._history) - 1:
            trimmed = list(self._history)[:self._history_pos + 1]
            self._history = deque(trimmed, maxlen=50)
        self._history.append(state)
        self._history_pos = len(self._history) - 1
        GLib.idle_add(self._update_history_ui)

    def _history_back(self):
        if self._history_pos > 0:
            self._history_pos -= 1
            self._restore_history(self._history_pos)

    def _history_forward(self):
        if self._history_pos < len(self._history) - 1:
            self._history_pos += 1
            self._restore_history(self._history_pos)

    def _restore_history(self, pos):
        state = list(self._history)[pos]
        mode, query, genre, page, label = state
        self._history_jump = True
        self._mode          = mode
        self._query_text    = query
        self._current_genre = genre
        self._current_page  = page
        # Uaktualnij UI wyszukiwarki
        if mode == MODE_ARTIST or mode == MODE_SEARCH:
            GLib.idle_add(self._search_entry.set_text, query)
        elif mode == MODE_GENRE:
            # Ustaw gatunek w combo
            def _set_combo():
                model = self._genre_combo.get_model()
                for i, row in enumerate(model):
                    if row[0] == genre:
                        self._genre_combo.set_active(i)
                        break
            GLib.idle_add(_set_combo)
        self._fetch_current()
        self._history_jump = False
        GLib.idle_add(self._update_history_ui)

    def _update_history_ui(self):
        pos  = self._history_pos
        hist = list(self._history)
        self._btn_back.set_sensitive(pos > 0)
        self._btn_fwd.set_sensitive(pos < len(hist) - 1)
        if 0 <= pos < len(hist):
            self._hist_lbl.set_text(hist[pos][4])  # label

    def _fetch_current(self):
        p = self._current_page
        filters = self._get_api_filters()

        if self._mode == MODE_FEATURED:
            ft = self._feed_type_combo.get_active_text() or ""
            if ft and not ft.startswith("—"):
                filters["type"] = ft
            label = f"Feed ({ft or 'domyślny'})  •  s.{p}"
            self._set_status(f"Wczytuję feed strona {p}…")
            threading.Thread(target=self._bg_load_featured, args=(p, filters), daemon=True).start()

        elif self._mode == MODE_ARTIST:
            atype = self._artist_type_combo.get_active_text() or "tracks"
            label = f"@{self._query_text} [{atype}] s.{p}"
            self._set_status(f"Wczytuję artystę '{self._query_text}' [{atype}] strona {p}…")
            threading.Thread(target=self._bg_load_artist, args=(self._query_text, atype, p, filters), daemon=True).start()

        elif self._mode == MODE_SEARCH:
            label = f"Szukaj: {self._query_text}  s.{p}"
            self._set_status(f"Szukam '{self._query_text}' strona {p}…")
            threading.Thread(target=self._bg_search, args=(self._query_text, p, filters), daemon=True).start()

        elif self._mode == MODE_GENRE:
            label = f"Gatunek: {self._current_genre}  s.{p}"
            self._set_status(f"Wczytuję '{self._current_genre}' strona {p}…")
            threading.Thread(target=self._bg_load_genre, args=(self._current_genre, p, filters), daemon=True).start()
        else:
            return

        GLib.idle_add(self._mode_lbl.set_text, label)
        self._push_history(label)

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
            title     = t.get("title") or "Bez tytułu"
            user      = t.get("user", {})
            artist    = user.get("username", "") if isinstance(user, dict) else ""
            username  = artist
            cover_url = t.get("artwork_url") or t.get("thumb") or ""
            if not cover_url and isinstance(user, dict):
                cover_url = user.get("avatar_url", "")
            try:
                secs = int(t.get("duration", 0))
            except Exception:
                secs = 0
            dur_str = fmt_dur(secs)
            dl_url   = t.get("download_url", "")
            # sprawdź czy plik jest już w cache
            cache_path = self._cache_path_for(url)
            local_path = self._local_path_for(title, artist)
            if local_path and os.path.exists(local_path):
                dl_state = "local"
            elif cache_path and os.path.exists(cache_path):
                dl_state = "cached"
            elif dl_url:
                dl_state = "yes"
            else:
                dl_state = ""
            self._store.append([title, artist, dur_str, url, secs, cover_url, username, None, dl_url, dl_state])
            count += 1

        self._page_lbl.set_text(f"Strona {page}" if page else "")
        self._btn_prev.set_sensitive(bool(page and page > 1))
        self._btn_next.set_sensitive(count == PAGE_SIZE)
        self._status_lbl.set_text(f"{status}  —  {count} utworów")

        # wyczyść lokalny filtr przy nowym załadowaniu
        self._local_filter.set_text("")

        # Zbierz (row_index, cover_url) i załaduj miniatury asynchronicznie
        thumb_jobs = []
        for idx in range(len(self._store)):
            it = self._store.get_iter(Gtk.TreePath.new_from_indices([idx]))
            cu = self._store.get_value(it, self.COL_COVER)
            if cu:
                thumb_jobs.append((idx, cu))
        if thumb_jobs:
            threading.Thread(
                target=self._bg_load_thumbs,
                args=(thumb_jobs,),
                daemon=True
            ).start()

    # ── Helper ścieżek ───────────────────────────────────────

    def _cache_path_for(self, stream_url):
        """Ścieżka pliku cache na podstawie URL streamu."""
        import hashlib
        h = hashlib.md5(stream_url.encode()).hexdigest()[:16]
        os.makedirs(CACHE_DIR, exist_ok=True)
        return os.path.join(CACHE_DIR, f"{h}.mp3")

    def _local_path_for(self, title, artist):
        """Ścieżka docelowego pliku w ~/Muzyka/HearThis.at."""
        if not title:
            return None
        safe = re.sub(r'[\/:*?"<>|]', '_', f"{artist} - {title}"[:120])
        return os.path.join(DOWNLOAD_DIR, f"{safe}.mp3")

    # ── Ikona stanu pobierania w kolumnie ─────────────────────

    def _dl_cell_data_func(self, column, cell, model, it, data):
        """Wyświetl ikonę stanu pobierania dla wiersza."""
        state = model.get_value(it, self.COL_DLSTATE)
        icons = {
            "local":  "✅",
            "cached": "💾",
            "yes":    "⬇",
            "":       " ",
        }
        cell.set_property("text", icons.get(state, " "))

    # ── Kontekstowe menu — pobieranie ────────────────────────

    def _bg_load_thumbs(self, jobs):
        """
        Wczytuje miniatury okładek.
        jobs: lista (row_index: int, cover_url: str)
        Używa indeksów (nie TreeIter) — bezpieczne między wątkami.
        """
        THUMB_SIZE = 40
        for row_idx, cover_url in jobs:
            p = fetch_pixbuf(cover_url)
            if not p:
                continue
            try:
                w, h = p.get_width(), p.get_height()
                scale = THUMB_SIZE / max(w, h, 1)
                nw = max(1, int(w * scale))
                nh = max(1, int(h * scale))
                scaled = p.scale_simple(nw, nh, GdkPixbuf.InterpType.BILINEAR)
            except Exception as e:
                print(f"[HearThis] thumb scale error: {e}")
                continue

            def _set(idx=row_idx, pb=scaled):
                # Walidacja: czy store nadal ma ten wiersz?
                if idx >= len(self._store):
                    return
                path = Gtk.TreePath.new_from_indices([idx])
                try:
                    it = self._store.get_iter(path)
                    self._store.set_value(it, self.COL_PIXBUF, pb)
                except Exception:
                    pass
            GLib.idle_add(_set)

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

    # ── Klik w kolumnę "→ Profil" ─────────────────────────────

    def _on_artist_link_click(self, treeview, event):
        """Obsługa kliknięć LPM w kolumny klikalne: → Profil i ⬇."""
        if event.button != 1:
            return False
        path_info = treeview.get_path_at_pos(int(event.x), int(event.y))
        if not path_info:
            return False
        path, col, cx, cy = path_info
        cols = treeview.get_columns()

        # Kolumna → Profil
        if col == cols[self._col_go_index]:
            it = self._sort_model.get_iter(path)
            username = self._sort_model.get_value(it, self.COL_USERNAME)
            if username:
                self._search_entry.set_text(username)
                self._start_artist()
            return True

        # Kolumna ⬇ pobierania
        if hasattr(self, '_col_dl_index') and col == cols[self._col_dl_index]:
            self._on_dl_col_click(treeview, event, path)
            return True

        return False

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
        it      = self._sort_model.get_iter(path)
        username = self._sort_model.get_value(it, self.COL_USERNAME)
        dl_url   = self._sort_model.get_value(it, self.COL_DLURL)
        dl_state = self._sort_model.get_value(it, self.COL_DLSTATE)
        title    = self._sort_model.get_value(it, self.COL_TITLE)
        artist   = self._sort_model.get_value(it, self.COL_ARTIST)
        stream_url = self._sort_model.get_value(it, self.COL_URL)

        menu = Gtk.Menu()

        item_play = Gtk.MenuItem(label="▶  Odtwórz")
        item_play.connect("activate", lambda w: self._activate_path(path))
        menu.append(item_play)

        # Pre-buf play (zawsze dostępne)
        item_prebuf = Gtk.MenuItem(label="📥  Buforuj i odtwórz (seekable)")
        item_prebuf.connect("activate",
            lambda w, u=stream_url, t=title, a=artist: threading.Thread(
                target=self._prebuf_and_play, args=(u, t, a), daemon=True).start())
        menu.append(item_prebuf)

        item_queue = Gtk.MenuItem(label="➕  Dodaj do kolejki")
        item_queue.connect("activate", lambda w: self._add_path_to_queue(path))
        menu.append(item_queue)

        menu.append(Gtk.SeparatorMenuItem())

        # Pobieranie
        if dl_state == "local":
            item_open = Gtk.MenuItem(label="📂  Otwórz lokalizację pliku")
            local_p   = self._local_path_for(title, artist)
            item_open.connect("activate",
                lambda w, p=local_p: Gio.AppInfo.launch_default_for_uri(
                    "file://" + os.path.dirname(p), None))
            menu.append(item_open)
        elif dl_state == "cached":
            item_dl_lib = Gtk.MenuItem(label="📚  Skopiuj z cache do biblioteki")
            item_dl_lib.connect("activate",
                lambda w, u=stream_url, t=title, a=artist: threading.Thread(
                    target=self._copy_cache_to_library, args=(u, t, a, path), daemon=True).start())
            menu.append(item_dl_lib)
        elif dl_url:
            item_dl = Gtk.MenuItem(label="⬇  Pobierz do biblioteki (~/Muzyka/HearThis.at)")
            item_dl.connect("activate",
                lambda w, u=dl_url, t=title, a=artist: threading.Thread(
                    target=self._download_to_library, args=(u, t, a, path), daemon=True).start())
            menu.append(item_dl)

        if username:
            menu.append(Gtk.SeparatorMenuItem())
            item_artist = Gtk.MenuItem(label=f"🎵  Przejdź do artysty: @{username}")
            item_artist.connect("activate", lambda w, u=username: self._go_to_artist(u))
            menu.append(item_artist)

        menu.show_all()
        menu.popup_at_pointer(event)

    # ── Klik w kolumnę ⬇ ─────────────────────────────────────

    def _on_dl_col_click(self, treeview, event, path):
        it       = self._sort_model.get_iter(path)
        dl_url   = self._sort_model.get_value(it, self.COL_DLURL)
        dl_state = self._sort_model.get_value(it, self.COL_DLSTATE)
        title    = self._sort_model.get_value(it, self.COL_TITLE)
        artist   = self._sort_model.get_value(it, self.COL_ARTIST)
        stream_url = self._sort_model.get_value(it, self.COL_URL)
        if dl_state == "yes" and dl_url:
            threading.Thread(
                target=self._download_to_library,
                args=(dl_url, title, artist, path),
                daemon=True
            ).start()
        elif dl_state == "":
            # brak download_url — zaoferuj pre-buf do cache
            threading.Thread(
                target=self._prebuf_and_play,
                args=(stream_url, title, artist),
                daemon=True
            ).start()

    # ── Pobieranie i pre-buffering ────────────────────────────

    def _update_row_state(self, path, new_state):
        """Uaktualnij COL_DLSTATE dla wiersza (może być w sort_model)."""
        def _do():
            try:
                it = self._sort_model.get_iter(path)
                # Konwertuj do source store iter
                child_it = self._sort_model.convert_iter_to_child_iter(it)
                child_it2 = self._filter_model.convert_iter_to_child_iter(child_it)
                self._store.set_value(child_it2, self.COL_DLSTATE, new_state)
            except Exception as e:
                print(f"[HearThis] row state update error: {e}")
        GLib.idle_add(_do)

    def _prebuf_and_play(self, stream_url, title, artist):
        """
        Pobierz utwór do pliku cache, potem odtwórz z dysku — umożliwia seeking.
        Jeśli pre-buf toggle jest wyłączony — użyj normalnego odtwarzania.
        """
        if not self._predown_btn.get_active():
            # Normalny tryb — tylko resolve i play
            real_url = self._resolve_url(stream_url)
            if real_url:
                _s, fsize = self._check_seekable_and_size(real_url)
                GLib.idle_add(self._play_url, real_url, title, artist, "", 0, fsize)
            return

        GLib.idle_add(self._set_status, f"⏳ Buforuję: {title}…")
        cache_path = self._cache_path_for(stream_url)

        if not os.path.exists(cache_path):
            real_url = self._resolve_url(stream_url)
            if not real_url:
                GLib.idle_add(self._set_status, f"❌ Brak URL dla: {title}")
                return
            ok = self._download_file(real_url, cache_path)
            if not ok:
                GLib.idle_add(self._set_status, f"❌ Błąd buforowania: {title}")
                return

        local_uri = "file://" + cache_path
        GLib.idle_add(self._set_status, f"▶ Odtwarzam z cache: {title}")
        # Pobierz cover_url z _cover_map lub zostaw puste
        cover_url = self._cover_map.get(stream_url, "")
        # Plik lokalny — pełny seeking
        dur_sec   = self._get_duration_from_cache(cache_path)
        fsize     = os.path.getsize(cache_path)
        GLib.idle_add(self._play_url, local_uri, title, artist, cover_url, dur_sec, fsize)

    def _get_duration_from_cache(self, path):
        """Szacuj czas trwania z rozmiaru MP3 (założenie: 128 kbps)."""
        try:
            size = os.path.getsize(path)
            return max(1, int(size * 8 / 128000))
        except Exception:
            return 0

    def _download_file(self, url, dest_path, progress_cb=None):
        """Pobierz plik z URL do dest_path. Zwraca True/False."""
        tmp = dest_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                total = int(resp.headers.get("Content-Length", 0) or 0)
                done  = 0
                with open(tmp, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if progress_cb and total:
                            progress_cb(done, total)
            shutil.move(tmp, dest_path)
            print(f"[HearThis] Downloaded: {dest_path} ({done} bytes)")
            return True
        except Exception as e:
            print(f"[HearThis] Download error: {e}")
            if os.path.exists(tmp):
                os.remove(tmp)
            return False

    def _download_to_library(self, dl_url, title, artist, path=None):
        """Pobierz utwór do ~/Muzyka/HearThis.at/."""
        local_path = self._local_path_for(title, artist)
        if not local_path:
            return
        if os.path.exists(local_path):
            GLib.idle_add(self._set_status, f"✅ Już pobrane: {title}")
            if path:
                self._update_row_state(path, "local")
            return

        GLib.idle_add(self._set_status, f"⬇ Pobieranie: {title}…")

        def _progress(done, total):
            pct = int(done * 100 / total)
            GLib.idle_add(self._set_status, f"⬇ {title}  {pct}%  ({done//1024} kB)")

        ok = self._download_file(dl_url, local_path, _progress)
        if ok:
            GLib.idle_add(self._set_status, f"✅ Pobrano: {title}")
            if path:
                self._update_row_state(path, "local")
            # Dodaj do biblioteki RB
            GLib.idle_add(self._add_to_rb_library, local_path)
        else:
            GLib.idle_add(self._set_status, f"❌ Błąd pobierania: {title}")

    def _copy_cache_to_library(self, stream_url, title, artist, path=None):
        """Skopiuj plik z cache do biblioteki."""
        cache_path = self._cache_path_for(stream_url)
        local_path = self._local_path_for(title, artist)
        if not local_path or not os.path.exists(cache_path):
            return
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        shutil.copy2(cache_path, local_path)
        GLib.idle_add(self._set_status, f"✅ Skopiowano do biblioteki: {title}")
        if path:
            self._update_row_state(path, "local")
        GLib.idle_add(self._add_to_rb_library, local_path)

    def _add_to_rb_library(self, file_path):
        """Dodaj pobrany plik MP3 do biblioteki Rhythmbox."""
        try:
            uri = "file://" + file_path
            self._db.entry_new(RB.RhythmDBEntryType.get_by_name(self._db, "song"), uri)
            self._db.commit()
            print(f"[HearThis] Added to library: {uri}")
        except Exception as e:
            print(f"[HearThis] Library add error: {e}")

    def _download_all_visible(self):
        """Pobierz wszystkie dostępne (dl_state='yes') z aktualnego widoku."""
        jobs = []
        for i in range(len(self._store)):
            path = Gtk.TreePath.new_from_indices([i])
            try:
                it   = self._filter_model.get_iter(path)
                if not self._filter_model.iter_is_valid(it):
                    continue
                dl_u = self._store.get_value(
                    self._filter_model.convert_iter_to_child_iter(it), self.COL_DLURL)
                st   = self._store.get_value(
                    self._filter_model.convert_iter_to_child_iter(it), self.COL_DLSTATE)
                t    = self._store.get_value(
                    self._filter_model.convert_iter_to_child_iter(it), self.COL_TITLE)
                a    = self._store.get_value(
                    self._filter_model.convert_iter_to_child_iter(it), self.COL_ARTIST)
            except Exception:
                continue
            if st == "yes" and dl_u:
                jobs.append((dl_u, t, a))

        if not jobs:
            self._set_status("Brak dostępnych do pobrania utworów w widoku.")
            return
        self._set_status(f"⬇ Pobieranie {len(jobs)} utworów…")

        def _dl_all():
            for dl_u, t, a in jobs:
                self._download_to_library(dl_u, t, a)
        threading.Thread(target=_dl_all, daemon=True).start()

    def _go_to_artist(self, username):
        self._search_entry.set_text(username)
        self._start_artist()

    def _activate_path(self, path):
        it = self._sort_model.get_iter(path)
        url       = self._sort_model.get_value(it, self.COL_URL)
        title     = self._sort_model.get_value(it, self.COL_TITLE)
        artist    = self._sort_model.get_value(it, self.COL_ARTIST)
        cover_url = self._sort_model.get_value(it, self.COL_COVER)
        dur_sec   = self._sort_model.get_value(it, self.COL_DURSEC)
        if url:
            if self._predown_btn.get_active():
                threading.Thread(
                    target=self._prebuf_and_play,
                    args=(url, title, artist),
                    daemon=True
                ).start()
            else:
                threading.Thread(
                    target=self._resolve_and_play,
                    args=(url, title, artist, cover_url, dur_sec),
                    daemon=True
                ).start()

    def _add_path_to_queue(self, path):
        it = self._sort_model.get_iter(path)
        url       = self._sort_model.get_value(it, self.COL_URL)
        title     = self._sort_model.get_value(it, self.COL_TITLE)
        artist    = self._sort_model.get_value(it, self.COL_ARTIST)
        cover_url = self._sort_model.get_value(it, self.COL_COVER)
        dur_sec   = self._sort_model.get_value(it, self.COL_DURSEC)
        if url:
            threading.Thread(
                target=self._resolve_and_queue,
                args=(url, title, artist, cover_url, dur_sec),
                daemon=True
            ).start()

    # ── Odtwarzanie ───────────────────────────────────────────

    def _on_row_activated(self, treeview, path, column):
        self._activate_path(path)

    def _resolve_and_play(self, stream_url, title, artist, cover_url="", dur_sec=0):
        play_url = self._resolve_url(stream_url)
        if not play_url:
            return
        # Sprawdź seekowalność i rozmiar TUTAJ (wątek tła, nie blokuje UI)
        _seekable, fsize = self._check_seekable_and_size(play_url)
        GLib.idle_add(self._play_url, play_url, title, artist, cover_url, dur_sec, fsize)

    def _resolve_and_queue(self, stream_url, title, artist, cover_url="", dur_sec=0):
        play_url = self._resolve_url(stream_url)
        if not play_url:
            return
        _seekable, fsize = self._check_seekable_and_size(play_url)
        GLib.idle_add(self._queue_url, play_url, title, artist, cover_url, dur_sec, fsize)

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

    def _check_seekable_and_size(self, url):
        """
        Wyślij HEAD z Range: bytes=0-0.
        Zwraca (seekable: bool, file_size: int).
        GStreamer będzie seekował tylko jeśli serwer zwróci:
          Accept-Ranges: bytes  +  Content-Length: N
        """
        try:
            # Próba 1: HEAD z Range
            req = urllib.request.Request(url, method="HEAD", headers={
                "User-Agent": "Mozilla/5.0",
                "Range": "bytes=0-0",
                "Icy-MetaData": "0",
            })
            with urllib.request.urlopen(req, timeout=6) as resp:
                accept = resp.headers.get("Accept-Ranges", "").lower()
                cl     = resp.headers.get("Content-Length", "") or resp.headers.get("X-Content-Length", "")
                cr     = resp.headers.get("Content-Range", "")
                seekable = (accept == "bytes") or bool(cr)
                try:
                    size = int(cl)
                except Exception:
                    size = 0
                print(f"[HearThis] HEAD: Accept-Ranges={accept!r} CL={cl!r} CR={cr!r} -> seekable={seekable} size={size}")
                return seekable, size
        except Exception as e:
            print(f"[HearThis] HEAD error: {e}")
        # Próba 2: GET pierwszych 16 bajtów
        try:
            req2 = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Range": "bytes=0-15",
            })
            with urllib.request.urlopen(req2, timeout=6) as resp:
                accept = resp.headers.get("Accept-Ranges", "").lower()
                cr     = resp.headers.get("Content-Range", "")
                cl     = resp.headers.get("Content-Length", "")
                seekable = (accept == "bytes") or bool(cr)
                try:
                    # Content-Range: bytes 0-15/TOTAL_SIZE
                    total = int(cr.split("/")[-1]) if "/" in cr else int(cl or 0)
                except Exception:
                    total = 0
                resp.read()
                print(f"[HearThis] GET Range: seekable={seekable} total={total}")
                return seekable, total
        except Exception as e:
            print(f"[HearThis] GET Range error: {e}")
        return False, 0

    def _ensure_entry(self, url, title, artist, cover_url="", dur_sec=0, file_size=0):
        """
        Tworzy wpis RhythmDB z pełnymi metadanymi.
        dur_sec MUSI być ustawiony — bez tego GStreamer nie może seekować.
        """
        if not url:
            return None
        db = self._db
        entry = db.entry_lookup_by_location(url)
        if entry is not None:
            # Uaktualnij duration jeśli poprzednio było 0
            if dur_sec > 0:
                try:
                    cur_dur = db.entry_get(entry, RB.RhythmDBPropType.DURATION)
                    if not cur_dur:
                        db.entry_set(entry, RB.RhythmDBPropType.DURATION, dur_sec)
                        db.commit()
                except Exception:
                    pass
            return entry

        methods = [
            ("db.entry_new",          lambda: db.entry_new(self._entry_type, url)),
            ("RB.RhythmDB.entry_new", lambda: RB.RhythmDB.entry_new(db, self._entry_type, url)),
            ("RB.RhythmDBEntry.new",  lambda: RB.RhythmDBEntry.new(db, self._entry_type, url)),
        ]
        entry = None
        for name, fn in methods:
            try:
                entry = fn()
                if entry is not None:
                    print(f"[HearThis] entry_new OK via {name}")
                    break
                print(f"[HearThis] {name} returned None")
            except Exception as e:
                print(f"[HearThis] {name}: {type(e).__name__}: {e}")

        if entry is None:
            print(f"[HearThis] BŁĄD: wszystkie entry_new zawiodły dla {url}")
            return None

        db.entry_set(entry, RB.RhythmDBPropType.TITLE,             title)
        db.entry_set(entry, RB.RhythmDBPropType.ARTIST,            artist)
        db.entry_set(entry, RB.RhythmDBPropType.ALBUM,             "hearthis.at")
        db.entry_set(entry, RB.RhythmDBPropType.STREAM_SONG_TITLE, title)

        # KLUCZOWE dla seekowania: czas trwania musi być w DB przed play()
        if dur_sec > 0:
            db.entry_set(entry, RB.RhythmDBPropType.DURATION, dur_sec)
            print(f"[HearThis] Duration set: {dur_sec}s")

        # Rozmiar pliku — pomaga GStreamer wyliczyć bitrate i pozycję seek
        if file_size > 0:
            try:
                db.entry_set(entry, RB.RhythmDBPropType.FILE_SIZE, file_size)
                print(f"[HearThis] FileSize set: {file_size} bytes")
            except Exception as e:
                print(f"[HearThis] FILE_SIZE error: {e}")

        # Zapamiętaj cover_url w mapie (będzie użyta gdy piosenka zacznie grać)
        if cover_url:
            self._cover_map[url] = cover_url

        db.commit()
        return entry

    def _play_url(self, url, title, artist, cover_url="", dur_sec=0, file_size=0):
        if not url:
            return
        print(f"[HearThis] Play: {url} dur={dur_sec}s size={file_size}")
        player = self._shell.props.shell_player
        entry = self._ensure_entry(url, title, artist, cover_url, dur_sec, file_size)
        if entry:
            try:
                player.play_entry(entry, self)
                self._pending_dur_entry = entry  # czekaj na potwierdzenie dur
                return
            except Exception as e:
                print(f"[HearThis] play_entry error: {e}")
        # Fallback bez wpisu DB
        try:
            player.load_uri(url, self, None)
        except Exception as e:
            print(f"[HearThis] load_uri error: {e}")
            self._set_status(f"Błąd odtwarzania: {e}")


    def _queue_url(self, url, title, artist, cover_url="", dur_sec=0, file_size=0):
        if not url:
            return
        print(f"[HearThis] Queue: {url}")
        entry = self._ensure_entry(url, title, artist, cover_url, dur_sec, file_size)
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
