# HearThis.at — Plugin do Rhythmbox

Streamuj muzykę z [hearthis.at](https://hearthis.at) bezpośrednio w Rhythmbox.

## Funkcje

| Funkcja | Opis |
|---|---|
| 🔍 **Szukaj artysty** | Wpisz nazwę użytkownika hearthis.at i wczytaj wszystkie jego utwory |
| 🔎 **Szukaj utworów** | Szukaj po tytule, słowie kluczowym lub nazwie |
| 🎵 **Przeglądaj gatunki** | Wybierz gatunek z listy i paginiuj wyniki |
| 🖼️ **Info o artyście** | Avatar i opis artysty wyświetlane przy wyszukiwaniu |
| ▶️ **Odtwarzanie** | Pełna integracja z playerem Rhythmbox (kolejka, skip, itp.) |

## Wymagania

- Rhythmbox 3.x (z obsługą pluginów Python 3)
- Python 3.6+
- Pakiety systemowe: `gir1.2-rb-3.0`, `gir1.2-gdkpixbuf-2.0`

Na Ubuntu/Debian:
```bash
sudo apt install rhythmbox-plugins python3-gi gir1.2-rb-3.0
```

## Instalacja

```bash
cd hearthis-rhythmbox-plugin
chmod +x install.sh
./install.sh
```

Następnie w Rhythmbox:
1. **Edit → Plugins**
2. Zaznacz **HearThis.at**
3. W lewym panelu pojawi się nowe źródło **HearThis.at**

## Użycie

```
┌─────────────────────────────────────────────────┐
│  [ szukaj... ] [Load Artist] [Search Tracks]    │
│  Genre: [electro ▼] [Browse Genre] [◀] [▶]      │
│  ┌─ avatar ─┐  Opis artysty...                  │
│  │          │                                   │
│  └──────────┘                                   │
│  Status: Loaded 20 tracks                       │
│  ┌──────────────────────────────────────────┐   │
│  │ Title          │ Artist    │ Duration    │   │
│  │ Track 1        │ djname    │ 5:23        │   │
│  │ Track 2        │ djname    │ 7:01        │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

- **Load Artist** — wpisz `username` artysty z hearthis.at (np. `mightyfools`)
- **Search Tracks** — szuka po słowie kluczowym w całym serwisie
- **Browse Genre** — wczytuje utwory z wybranego gatunku, stronicowanie ◀▶
- Dwuklik na utwór — natychmiastowe odtwarzanie

## Struktura plików

```
hearthis/
├── hearthis.plugin   ← deskryptor pluginu
└── hearthis.py       ← kod źródłowy
```

## Rozwiązywanie problemów

Uruchom Rhythmbox z terminala, aby zobaczyć logi:
```bash
rhythmbox -D
```
Szukaj linii `[HearThis]` w wyjściu.
