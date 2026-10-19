import os
import math
import string
import time
import datetime
import json
import re
import unicodedata
import csv
import hmac
import io
import threading
import secrets
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import requests


def sort_key(title):
    """Sort key that ignores accents and case, so that 'Émissions' sorts
    between 'Animes' and 'Séries'. Without stripping the accents, 'é'
    (code point 233) sorts after 'z' and accented titles end up last.
    NFD splits 'é' into 'e' + a combining accent, which we then drop."""
    decomposed = unicodedata.normalize("NFD", title or "")
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()


from flask import Flask, Response, abort, flash, make_response, redirect, render_template, request, send_file, session, url_for
from plexapi.server import PlexServer

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)

APP_VERSION = os.environ.get("APP_VERSION", "dev")


@app.context_processor
def inject_version():
    return {"app_version": APP_VERSION}


# ---- optional password protection ------------------------------------------
# Set APP_PASSWORD to put the whole app behind a login page (useful if it is
# reachable from the Internet). Left empty/unset, nothing changes: no login.
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# very small brute-force guard: after LOGIN_MAX_FAILURES wrong passwords from
# one address, further attempts are refused for LOGIN_WINDOW seconds
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = 300
_LOGIN_FAILURES = {}


def _recent_failures(ip):
    now = time.time()
    recent = [t for t in _LOGIN_FAILURES.get(ip, []) if now - t < LOGIN_WINDOW]
    if recent:
        _LOGIN_FAILURES[ip] = recent
    else:
        _LOGIN_FAILURES.pop(ip, None)
    return recent


def _safe_next(target):
    """Only follow local paths after login (no open redirect)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("libraries")


@app.context_processor
def inject_auth():
    return {"auth_enabled": bool(APP_PASSWORD)}


# ---- "new version available" notice ----------------------------------------
# Compares APP_VERSION with the highest vX.Y.Z tag of the GitHub repo. The
# check runs in a background thread (a page never waits for GitHub), is cached
# for 6 hours and fails silently. Set DISABLE_UPDATE_CHECK=1 to turn it off
# (no request to GitHub at all). Builds without a version ("dev", bare commit
# hash) are never compared.
GITHUB_REPO = "Minimaxpower/ArtLayerr"
UPDATE_CHECK_ENABLED = os.environ.get("DISABLE_UPDATE_CHECK", "").strip().lower() not in ("1", "true", "yes", "on")
UPDATE_CHECK_INTERVAL = 6 * 3600
UPDATE_RETRY_INTERVAL = 30 * 60
_UPDATE_STATE = {"latest": None, "next_check": 0.0, "running": False}
_UPDATE_LOCK = threading.Lock()
_TAG_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")           # a release tag, no suffix
_VERSION_PREFIX_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")  # also "v2.0.0-3-gabc1234"


def _version_tuple(text, strict=False):
    m = (_TAG_RE if strict else _VERSION_PREFIX_RE).match((text or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def _fetch_latest_tag():
    resp = requests.get(
        f"https://api.github.com/repos/{GITHUB_REPO}/tags",
        params={"per_page": 100},
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ArtLayerr"},
        timeout=5,
    )
    resp.raise_for_status()
    best = None
    for tag in resp.json():
        name = tag.get("name", "")
        v = _version_tuple(name, strict=True)
        if v and (best is None or v > best[0]):
            best = (v, name)
    return best[1] if best else None


def _refresh_latest_version():
    try:
        latest, ok = _fetch_latest_tag(), True
    except Exception:
        latest, ok = None, False
    with _UPDATE_LOCK:
        if ok:
            _UPDATE_STATE["latest"] = latest
            _UPDATE_STATE["next_check"] = time.time() + UPDATE_CHECK_INTERVAL
        else:
            _UPDATE_STATE["next_check"] = time.time() + UPDATE_RETRY_INTERVAL
        _UPDATE_STATE["running"] = False


def get_update_info():
    """{'version', 'url'} when a newer release tag exists, else None."""
    if not UPDATE_CHECK_ENABLED:
        return None
    current = _version_tuple(APP_VERSION)
    if current is None:
        return None
    with _UPDATE_LOCK:
        if time.time() >= _UPDATE_STATE["next_check"] and not _UPDATE_STATE["running"]:
            _UPDATE_STATE["running"] = True
            threading.Thread(target=_refresh_latest_version, daemon=True).start()
        latest = _UPDATE_STATE["latest"]
    latest_t = _version_tuple(latest, strict=True) if latest else None
    if latest_t and latest_t > current:
        return {"version": latest, "url": f"https://github.com/{GITHUB_REPO}/releases/tag/{latest}"}
    return None


@app.context_processor
def inject_update():
    return {"update_info": get_update_info()}


# ---- display settings (per browser, kept in cookies) -------------------------
# Logos and square art are optional: most people don't use them, so they are
# hidden (chips, filters, dashboard rings, export columns) unless switched on
# in the Settings menu. Everything is still scanned, so switching them on needs
# no new check.
ART_TYPES = ("poster", "background", "logo", "square")
ART_EXTRAS = ("logo", "square")


# Accessibility prefs (cookies too): colorblind palette + extra visual cues.
CB_MODES = ("off", "rg", "by")  # default, red-green, blue-yellow
CUE_MODES = ("none", "icons", "patterns")


def _pref_choice(name, allowed, default):
    value = request.cookies.get(f"artlayerr_{name}")
    return value if value in allowed else default


def enabled_art_types():
    return [t for t in ART_EXTRAS if request.cookies.get(f"artlayerr_show_{t}") == "1"]


def active_art_types():
    """Enabled in the settings AND present in the last scan of the assets."""
    return enabled_art_types() if _ASSET_CACHE.get("art_scanned") else []


@app.context_processor
def inject_art_prefs():
    enabled = enabled_art_types()
    scanned = bool(_ASSET_CACHE.get("art_scanned"))
    checked = _ASSET_CACHE.get("computed_at") is not None
    return {
        "show_logo": "logo" in enabled,
        "show_square": "square" in enabled,
        "cb_mode": _pref_choice("cb", CB_MODES, "off"),
        "cue_mode": _pref_choice("cue", CUE_MODES, "none"),
        "art_scanned": scanned,
        # switched on, but the saved scan predates logo/square support: one Update fixes it
        "art_pending": bool(enabled) and checked and not scanned,
    }


@app.route("/settings", methods=["POST"])
def save_settings():
    resp = redirect(_safe_next(request.form.get("next")))
    for kind in ART_EXTRAS:
        resp.set_cookie(
            f"artlayerr_show_{kind}",
            "1" if request.form.get(f"show_{kind}") else "0",
            max_age=PREF_COOKIE_MAX_AGE,
            samesite="Lax",
        )
    for name, allowed, default in (("cb", CB_MODES, "off"), ("cue", CUE_MODES, "none")):
        value = request.form.get(name)
        if value not in allowed:
            value = default
        resp.set_cookie(
            f"artlayerr_{name}",
            value,
            max_age=PREF_COOKIE_MAX_AGE,
            samesite="Lax",
        )
    return resp


@app.before_request
def require_password():
    if not APP_PASSWORD:
        return None
    if request.endpoint in ("login", "static"):
        return None
    if session.get("authenticated"):
        return None
    target = None
    if request.method == "GET":
        target = request.path
        if request.query_string:
            target += "?" + request.query_string.decode("utf-8", "replace")
    return redirect(url_for("login", next=target))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("connect"))
    next_url = _safe_next(request.values.get("next"))
    error = None
    if request.method == "POST":
        ip = request.remote_addr or "?"
        if len(_recent_failures(ip)) >= LOGIN_MAX_FAILURES:
            error = "Too many attempts. Try again in a few minutes."
        else:
            supplied = request.form.get("password", "")
            if hmac.compare_digest(supplied.encode("utf-8"), APP_PASSWORD.encode("utf-8")):
                _LOGIN_FAILURES.pop(ip, None)
                forget_token()
                session.clear()
                session["authenticated"] = True
                return redirect(next_url)
            _LOGIN_FAILURES.setdefault(ip, []).append(time.time())
            error = "Wrong password."
    return render_template("login.html", error=error, next_url=next_url)


@app.route("/logout")
def logout():
    forget_token()
    session.clear()
    return redirect(url_for("login") if APP_PASSWORD else url_for("connect"))


# module‑level alphabet list so we only build it once
ALPHABET = list(string.ascii_uppercase)
ALPHABET.insert(0, "0-9")

# ---- list sorting and page size -------------------------------------------
# Both are remembered in plain cookies (not the login session), so they stick
# between visits AND reach the item page without being threaded through every
# URL: the item page's Previous/Next must walk the list in the same order the
# grid shows, and both read the same cookie.
SORT_OPTIONS = {
    "title": "Title A–Z",
    "title_desc": "Title Z–A",
    "added": "Recently added",
    "added_asc": "Oldest added",
    "year_desc": "Year (newest)",
    "year_asc": "Year (oldest)",
}
DEFAULT_SORT = "title"
PER_PAGE_OPTIONS = [12, 24, 48, 96]  # all multiples of 6: no orphan row on the desktop grid
DEFAULT_PER_PAGE = 12
PREF_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def get_sort_pref():
    """Sort mode: ?sort= wins (the dropdown), then the cookie, then the default."""
    value = request.args.get("sort") or request.cookies.get("artlayerr_sort")
    return value if value in SORT_OPTIONS else DEFAULT_SORT


def get_per_page_pref():
    raw = request.args.get("per_page") or request.cookies.get("artlayerr_per_page")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PER_PAGE
    return value if value in PER_PAGE_OPTIONS else DEFAULT_PER_PAGE


def sort_items(items, sort):
    """Order Plex items for the chosen mode. 'title' keeps Plex's own order, so
    the default view is exactly what it was before. Python's sort is stable,
    so items that tie (same year, same added date) stay in title order. Items
    with no year always go last."""
    if sort == "title_desc":
        return list(reversed(items))
    if sort in ("added", "added_asc"):
        return sorted(
            items,
            key=lambda i: getattr(i, "addedAt", None) or datetime.datetime.min,
            reverse=(sort == "added"),
        )
    if sort in ("year_desc", "year_asc"):
        known = [i for i in items if getattr(i, "year", None)]
        unknown = [i for i in items if not getattr(i, "year", None)]
        known.sort(key=lambda i: i.year, reverse=(sort == "year_desc"))
        return known + unknown
    return list(items)

# cache of non-empty asset folders, split between poster files and background
# files, per media type. Scanning this means checking every item's folder on
# disk, which is the expensive part of this app — unlike before, this is now
# MANUAL by default (AUTO_REFRESH_HOURS adds an optional timer): it is never
# recomputed automatically on a page view or after
# a timeout. It is only (re)computed when the "🔄 Update" button
# is used, or right after a download (a single, cheap, known-fresh update).
# computed_at stays None until the first manual check ever happens.
_ASSET_CACHE = {
    "movie_posters": set(),
    "movie_backgrounds": set(),
    "series_posters": set(),
    "series_backgrounds": set(),
    "series_season_episode": {},
    "movie_logos": set(),
    "movie_squares": set(),
    "series_logos": set(),
    "series_squares": set(),
    # False for a scan saved before logo/square support existed: those flags
    # then stay "unknown" (?) until the next Update instead of looking missing
    "art_scanned": False,
    "computed_at": None,
    # bumped when only ONE folder was re-read (after a download): it marks the
    # cached "missing" lists as stale without pretending that a full check ran,
    # so "Last checked" keeps showing the time of the last full scan
    "changed_at": None,
}

_SEASON_FILE_RE = re.compile(r"^season(\d+)", re.IGNORECASE)
_EPISODE_FILE_RE = re.compile(r"^s\d+e\d+", re.IGNORECASE)


# How many folder levels below ASSETS_DIR are searched for asset folders (default 2):
#   assets/<Title>/poster.jpg                       (1 level)
#   assets/movies/<Title>/poster.jpg                (2 levels, ArtLayerr's own layout)
#   assets/<Library>/<Title {tmdb-1}>/poster.jpg    (2 levels, typical Kometa layout)
#   assets/<Library>/<Group>/<Title>/poster.jpg    (3 levels: needs ASSETS_DEPTH=3)
try:
    ASSETS_DEPTH = max(1, int(os.environ.get("ASSETS_DEPTH") or 2))
except ValueError:
    ASSETS_DEPTH = 2


def _is_visible_dir(entry):
    """Skip hidden folders and NAS housekeeping ones (@eaDir, #recycle, ...)."""
    try:
        return entry.is_dir() and not entry.name.startswith((".", "@", "#"))
    except OSError:
        return False


def _scan_tree(root, depth=None):
    """Walk `root` down to `depth` levels and find every asset folder, whatever
    the layout: a folder directly containing poster.* / background.* / logo.* /
    square.* or Season01.* / S01E01.* files. A folder is not searched further
    once it is recognised as an asset folder.

    Returns (records, dirs):
      records: folder name -> {"kinds", "seasons", "episode_count", "hints"}
        hints = {"movies"} / {"series"} when the folder sits under a top-level
        folder literally called movies / series (ArtLayerr's own layout), and
        {""} for any other layout (kind unknown, so usable for both)
      dirs: folder name -> [absolute paths]  (every folder met, even empty ones)
    """
    depth = depth or ASSETS_DEPTH
    records = {}
    dirs = {}

    def visit(path, level, top):
        try:
            with os.scandir(path) as it:
                subdirs = [e for e in it if _is_visible_dir(e)]
        except (FileNotFoundError, PermissionError, NotADirectoryError):
            return
        for e in subdirs:
            group = top or e.name
            dirs.setdefault(e.name, []).append(e.path)
            kinds, seasons, episode_count = _scan_one_folder(e.path)
            if kinds or seasons or episode_count:
                rec = records.setdefault(
                    e.name,
                    {"kinds": set(), "seasons": set(), "episode_count": 0, "hints": set()},
                )
                rec["kinds"] |= kinds
                rec["seasons"] |= seasons
                rec["episode_count"] += episode_count
                low = group.lower()
                rec["hints"].add(low if low in ("movies", "series") else "")
            elif level + 1 < depth:
                visit(e.path, level + 1, group)

    visit(root, 0, None)
    return records, dirs


def _apply_scan(records):
    """Fill the cached sets from the records of _scan_tree."""
    for prefix in ("movie", "series"):
        for t in ("posters", "backgrounds", "logos", "squares"):
            _ASSET_CACHE[f"{prefix}_{t}"] = set()
    _ASSET_CACHE["series_season_episode"] = {}
    for name, rec in records.items():
        for_movie = bool(rec["hints"] & {"movies", ""})
        for_series = bool(rec["hints"] & {"series", ""})
        for t in rec["kinds"]:
            if for_movie:
                _ASSET_CACHE[f"movie_{t}"].add(name)
            if for_series:
                _ASSET_CACHE[f"series_{t}"].add(name)
        if for_series and (rec["seasons"] or rec["episode_count"]):
            _ASSET_CACHE["series_season_episode"][name] = {
                "seasons": rec["seasons"],
                "episode_count": rec["episode_count"],
            }


# ---- matching Plex items to asset folders -------------------------------------
# Asset folders are matched, in this order, by (1) the exact name of the media
# folder in Plex (the historical rule), (2) an external id written in the folder
# name, Kometa style: "Title (2016) {tmdb-333371}", "{tvdb-323168}", "{imdb-tt...}"
# (also "[tmdbid-123]"), (3) "Title (Year)" with any {...} / [...] tag removed.
_ID_TAG_RE = re.compile(r"[{\[](tmdb|tvdb|imdb)(?:id)?[-=]([A-Za-z0-9]+)[}\]]", re.IGNORECASE)
_TAG_GROUP_RE = re.compile(r"\{[^}]*\}|\[[^\]]*\]")
_ASSET_INDEX = {"names": set(), "ids": {}, "titles": {}}


def _title_key(text):
    return sort_key(re.sub(r"\s+", " ", _TAG_GROUP_RE.sub(" ", text or "")).strip())


def _rebuild_asset_index():
    """Derive the lookup tables from the cached scan (also run after load_state,
    so nothing extra has to be saved)."""
    names = set(_ASSET_CACHE["series_season_episode"])
    for prefix in ("movie", "series"):
        for t in ("posters", "backgrounds", "logos", "squares"):
            names |= _ASSET_CACHE[f"{prefix}_{t}"]
    ids, titles = {}, {}
    for name in sorted(names):
        for kind, value in _ID_TAG_RE.findall(name):
            ids.setdefault(f"{kind.lower()}-{value.lower()}", name)
        titles.setdefault(_title_key(name), name)
    _ASSET_INDEX["names"], _ASSET_INDEX["ids"], _ASSET_INDEX["titles"] = names, ids, titles


# folder name -> [absolute paths], from the last scan (None = not built yet).
# Rebuilt by every full scan; patched when a folder is created.
_DIR_INDEX = {"root": None, "map": None}


def _dir_paths(asset_root, name):
    if _DIR_INDEX["map"] is None or _DIR_INDEX["root"] != asset_root:
        _, dirs = _scan_tree(asset_root)
        _DIR_INDEX["root"], _DIR_INDEX["map"] = asset_root, dirs
    return list(_DIR_INDEX["map"].get(name, ()))


def _remember_dir(asset_root, path):
    if _DIR_INDEX["map"] is None or _DIR_INDEX["root"] != asset_root:
        return
    paths = _DIR_INDEX["map"].setdefault(os.path.basename(path), [])
    if path not in paths:
        paths.append(path)


def get_asset_folders(asset_root, force=False):
    """Return the cached scan. Never scans on its own — pass force=True to
    actually (re)do the disk scan (used by the manual refresh button and
    right after a download)."""
    if force:
        records, dirs = _scan_tree(asset_root)
        _apply_scan(records)
        _DIR_INDEX["root"], _DIR_INDEX["map"] = asset_root, dirs
        _rebuild_asset_index()
        _ASSET_CACHE["art_scanned"] = True
        _ASSET_CACHE["computed_at"] = time.time()
        save_state()
    return _ASSET_CACHE


def _scan_one_folder(path):
    """Read ONE asset folder with the same rules as the full scan. Returns
    (kinds, seasons, episode_count): kinds is a subset of posters / backgrounds /
    logos / squares."""
    kinds = set()
    seasons = set()
    episode_count = 0
    try:
        with os.scandir(path) as files:
            for f in files:
                if not f.is_file():
                    continue
                name = f.name.lower()
                if name.startswith("poster"):
                    kinds.add("posters")
                elif name.startswith(("square", "backgroundsquare")):
                    kinds.add("squares")
                elif name.startswith("background"):
                    kinds.add("backgrounds")
                elif name.startswith(("logo", "clearlogo")):
                    kinds.add("logos")
                m = _SEASON_FILE_RE.match(f.name)
                if m:
                    seasons.add(int(m.group(1)))
                elif _EPISODE_FILE_RE.match(f.name):
                    episode_count += 1
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        pass
    return kinds, seasons, episode_count


def update_asset_folder(path, kind):
    """Re-read a single asset folder (wherever it lives under the assets root)
    and patch the cached scan, instead of walking the whole assets tree. The
    caller saves the state."""
    name = os.path.basename(path)
    prefix = "movie" if kind == "movies" else "series"
    kinds, seasons, episode_count = _scan_one_folder(path)
    for t in ("posters", "backgrounds", "logos", "squares"):
        cached = _ASSET_CACHE[f"{prefix}_{t}"]
        if t in kinds:
            cached.add(name)
        else:
            cached.discard(name)
    if kind == "series":
        if seasons or episode_count:
            _ASSET_CACHE["series_season_episode"][name] = {
                "seasons": seasons,
                "episode_count": episode_count,
            }
        else:
            _ASSET_CACHE["series_season_episode"].pop(name, None)
    _rebuild_asset_index()
    _ASSET_CACHE["changed_at"] = time.time()


def _media_folder_name(item):
    """Name of the item's own folder in Plex (movie folder / show folder)."""
    try:
        if item.type == "movie":
            return os.path.basename(os.path.dirname(item.media[0].parts[0].file))
        return os.path.basename(item.locations[0])
    except Exception:
        return None


def _asset_name(item):
    """Name of the asset folder of this item. The Plex folder name when an asset
    folder of that name exists; otherwise the asset folder found through the
    item's TMDB / TVDB / IMDB id, or through "Title (Year)". When nothing
    matches, the Plex folder name is returned (it is where a download goes)."""
    name = _media_folder_name(item)
    idx = _ASSET_INDEX
    if name and name in idx["names"]:
        return name
    try:
        for kind, value in _item_external_ids(item).items():
            hit = idx["ids"].get(f"{kind}-{str(value).lower()}")
            if hit:
                return hit
        title = getattr(item, "title", None)
        year = getattr(item, "year", None)
        if title and year:
            hit = idx["titles"].get(_title_key(f"{title} ({year})"))
            if hit:
                return hit
    except Exception:
        pass
    return name


def annotate_asset_flags(items, asset_dir=None):
    """Set item.has_poster / item.has_background (and, for shows, custom
    season/episode info) for each item, from the cached scan. If no manual
    check has ever been run yet, everything is set to None (meaning "unknown
    / not checked"), never guessed as False."""
    # asset_dir is only passed by the background refresh (no request, no session)
    asset_root = os.path.join(os.getcwd(), asset_dir or session.get("asset_dir", "assets"))
    cache = get_asset_folders(asset_root)  # force=False: just reads, never scans
    checked = cache["computed_at"] is not None
    art_scanned = bool(cache.get("art_scanned"))
    for item in items:
        if not checked:
            item.has_poster = None
            item.has_background = None
            item.has_logo = None
            item.has_square = None
            item.custom_seasons = None
            item.custom_episode_count = None
            item.custom_seasons_display = None
            item.seasons_complete = None
            item.episodes_complete = None
            continue
        try:
            if item.type == "movie":
                asset_name = _asset_name(item)
                item.has_poster = asset_name in cache["movie_posters"]
                item.has_background = asset_name in cache["movie_backgrounds"]
                item.has_logo = (asset_name in cache["movie_logos"]) if art_scanned else None
                item.has_square = (asset_name in cache["movie_squares"]) if art_scanned else None
                item.custom_seasons = None
                item.custom_episode_count = None
                item.custom_seasons_display = None
                item.seasons_complete = None
                item.episodes_complete = None
            else:
                asset_name = _asset_name(item)
                item.has_poster = asset_name in cache["series_posters"]
                item.has_background = asset_name in cache["series_backgrounds"]
                item.has_logo = (asset_name in cache["series_logos"]) if art_scanned else None
                item.has_square = (asset_name in cache["series_squares"]) if art_scanned else None
                se_info = cache["series_season_episode"].get(asset_name)
                item.custom_seasons = se_info["seasons"] if se_info else set()
                item.custom_episode_count = se_info["episode_count"] if se_info else 0

                if item.custom_seasons:
                    nums = sorted(item.custom_seasons)
                    if len(nums) == 1:
                        item.custom_seasons_display = f"S{nums[0]:02d}"
                    elif nums == list(range(nums[0], nums[-1] + 1)):
                        item.custom_seasons_display = f"S{nums[0]:02d}-S{nums[-1]:02d}"
                    else:
                        item.custom_seasons_display = ",".join(f"S{n:02d}" for n in nums)
                else:
                    item.custom_seasons_display = None

                # complete = every season / every episode Plex knows about
                # actually has a custom file, not just "at least one"
                total_seasons = getattr(item, "childCount", None) or 0
                total_episodes = getattr(item, "leafCount", None) or 0
                item.seasons_complete = bool(total_seasons) and len(item.custom_seasons) >= total_seasons
                item.episodes_complete = bool(total_episodes) and item.custom_episode_count >= total_episodes
        except Exception:
            item.has_poster = False
            item.has_background = False
            item.has_logo = False
            item.has_square = False
            item.custom_seasons = set()
            item.custom_episode_count = 0
            item.custom_seasons_display = None
            item.seasons_complete = False
            item.episodes_complete = False
    return items


# cache of {(section_key, missing_type): {"keys": [ratingKey, ...], "scanned_at": ...}}
# — the ordered list of items missing a given attribute, for the "missing
# only" nav mode. Only rebuilt when it's older than the last manual asset
# check (i.e. right after the button is used, or after a download), never on
# a timer.
_MISSING_CACHE = {}
# The cached key list is only valid while the Plex library has not changed. A new
# or removed title does not touch the asset scan, so a time limit bounds how long
# a stale list can survive (the item page also rebuilds it when it spots a title
# the list does not know, see browse()).
_MISSING_CACHE_TTL = 300  # seconds

# which attribute each filter type checks, and whether it's show-only
MISSING_TYPES = {
    "poster": {"label": "posters", "show_only": False},
    "background": {"label": "backgrounds", "show_only": False},
    "logo": {"label": "logos", "show_only": False},
    "square": {"label": "square", "show_only": False},
    "season": {"label": "seasons", "show_only": True},
    "episode": {"label": "episodes", "show_only": True},
}


def missing_types_for(section_type):
    """Filters offered for a library: logo / square art only when switched on in
    the settings (and already scanned), season / episode only for shows."""
    types = ["poster", "background"] + active_art_types()
    if section_type == "show":
        types += ["season", "episode"]
    return types


def _is_missing(item, missing_type):
    if missing_type == "poster":
        return not item.has_poster
    if missing_type == "background":
        return not item.has_background
    if missing_type == "logo":
        return not item.has_logo
    if missing_type == "square":
        return not item.has_square
    # "missing" = not every season / episode has a custom file yet (this is
    # what the dashboard rings count too, so a ring click lists the same shows)
    if missing_type == "season":
        return not item.seasons_complete
    if missing_type == "episode":
        return not item.episodes_complete
    return False


# ---- "has" filters: the inverse of the "missing" ones ----------------------
# A list filter always travels in the same ?missing_type= parameter (grid, item
# page, download forms, redirects...). A "has_" prefix flips it: "poster" keeps
# the items MISSING a poster, "has_poster" the ones that HAVE one. Riding on
# the existing parameter means Previous/Next on the item page and every
# redirect work for the new filters without being touched.
HAS_PREFIX = "has_"


def filter_base(filter_type):
    """'has_poster' -> 'poster'; 'poster' -> 'poster'."""
    if filter_type.startswith(HAS_PREFIX):
        return filter_type[len(HAS_PREFIX):]
    return filter_type


def filter_types_for(section_type):
    """Every filter value accepted for a library: the missing ones and their has_ twins."""
    base = missing_types_for(section_type)
    return base + [HAS_PREFIX + t for t in base]


def _has(item, art_type):
    """The item has at least one such image. For seasons / episodes that means
    at least one, so a half-covered show is both in "Has seasons" and in
    "Missing seasons" (the latter means "not every season is covered")."""
    if art_type == "poster":
        return bool(item.has_poster)
    if art_type == "background":
        return bool(item.has_background)
    if art_type == "logo":
        return bool(item.has_logo)
    if art_type == "square":
        return bool(item.has_square)
    if art_type == "season":
        return bool(item.custom_seasons)
    if art_type == "episode":
        return bool(item.custom_episode_count)
    return False


def _matches(item, filter_type):
    """Whether the item passes a list filter, 'poster' or 'has_poster' style."""
    if filter_type.startswith(HAS_PREFIX):
        return _has(item, filter_type[len(HAS_PREFIX):])
    return _is_missing(item, filter_type)


def get_missing_rating_keys(section, missing_type="poster", force=False, sort=DEFAULT_SORT):
    if filter_base(missing_type) not in MISSING_TYPES:
        missing_type = "poster"
    if _ASSET_CACHE["computed_at"] is None:
        return None  # nothing has ever been checked — caller must handle this
    key = (str(section.key), missing_type, sort)
    entry = _MISSING_CACHE.get(key)
    if (
        force
        or not entry
        or entry["scanned_at"] < max(_ASSET_CACHE["computed_at"], _ASSET_CACHE.get("changed_at") or 0)
        or time.time() - entry["scanned_at"] > _MISSING_CACHE_TTL
    ):
        items = sort_items(list(section.all()), sort)
        annotate_asset_flags(items)
        entry = {
            "keys": [i.ratingKey for i in items if _matches(i, missing_type)],
            "scanned_at": time.time(),
        }
        _MISSING_CACHE[key] = entry
    return entry["keys"]


def refresh_asset_check(plex, section_key=None):
    """Run a full manual check: rescan the asset folders on disk, then rebuild
    the missing-item lists (every filter type) for the given section (or all
    known sections)."""
    asset_root = os.path.join(os.getcwd(), session.get("asset_dir", "assets"))
    get_asset_folders(asset_root, force=True)
    # No need to rebuild the missing-item lists here: bumping computed_at above
    # already marks every cached list as stale, and get_missing_rating_keys()
    # rebuilds only the one actually requested, the next time it's needed.
    # (Rebuilding all four eagerly meant up to four full-library Plex calls
    # after every single download.)


# dashboard stats are expensive (a full section.all() per movie/show library),
# so — same philosophy as the rest of this app — they're computed only on
# demand (first visit, or an explicit "Update" click), never on a timer.
_DASHBOARD_CACHE = {"data": None, "computed_at": None}

# keys (as text) of the libraries the user chose to leave out of the dashboard
# totals; kept in the state file so the choice survives a restart
_IGNORED = set()


# --- persistence -----------------------------------------------------------
# The verification results and the dashboard numbers live in memory, so every
# rebuild/restart used to reset them (all badges back to "?"). They're now also
# written to a small JSON file, by default inside the mounted assets folder
# (the one volume that survives a rebuild), and reloaded at startup.
# Override the location with the STATE_DIR environment variable if you'd
# rather keep it elsewhere. Any failure here is swallowed on purpose: losing
# the saved state must never break the app, it just means a fresh "Update".
_STATE_FILE_NAME = ".layerr-state.json"
_STATE_VERSION = 1


def _state_path():
    base = os.environ.get("STATE_DIR") or os.environ.get("KOMETA_ASSET_DIR", "assets")
    return os.path.join(os.getcwd(), base, _STATE_FILE_NAME)


def save_state():
    try:
        series_se = {
            name: {"seasons": sorted(info["seasons"]), "episode_count": info["episode_count"]}
            for name, info in _ASSET_CACHE["series_season_episode"].items()
        }
        payload = {
            "version": _STATE_VERSION,
            "ignored_libraries": sorted(_IGNORED),
            "asset_cache": {
                "movie_posters": sorted(_ASSET_CACHE["movie_posters"]),
                "movie_backgrounds": sorted(_ASSET_CACHE["movie_backgrounds"]),
                "series_posters": sorted(_ASSET_CACHE["series_posters"]),
                "series_backgrounds": sorted(_ASSET_CACHE["series_backgrounds"]),
                "series_season_episode": series_se,
                "movie_logos": sorted(_ASSET_CACHE["movie_logos"]),
                "movie_squares": sorted(_ASSET_CACHE["movie_squares"]),
                "series_logos": sorted(_ASSET_CACHE["series_logos"]),
                "series_squares": sorted(_ASSET_CACHE["series_squares"]),
                "art_scanned": bool(_ASSET_CACHE["art_scanned"]),
                "computed_at": _ASSET_CACHE["computed_at"],
            },
            "dashboard": {
                "data": _DASHBOARD_CACHE["data"],
                "computed_at": _DASHBOARD_CACHE["computed_at"],
            },
        }
        path = _state_path()
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp_path, path)  # atomic: never leaves a half-written file
    except Exception:
        pass


def load_state():
    try:
        with open(_state_path(), encoding="utf-8") as f:
            payload = json.load(f)
        if payload.get("version") != _STATE_VERSION:
            return
        ac = payload.get("asset_cache") or {}
        if ac.get("computed_at") is not None:
            _ASSET_CACHE["movie_posters"] = set(ac.get("movie_posters", []))
            _ASSET_CACHE["movie_backgrounds"] = set(ac.get("movie_backgrounds", []))
            _ASSET_CACHE["series_posters"] = set(ac.get("series_posters", []))
            _ASSET_CACHE["series_backgrounds"] = set(ac.get("series_backgrounds", []))
            _ASSET_CACHE["series_season_episode"] = {
                name: {
                    "seasons": set(info.get("seasons", [])),
                    "episode_count": int(info.get("episode_count", 0)),
                }
                for name, info in (ac.get("series_season_episode") or {}).items()
            }
            _ASSET_CACHE["movie_logos"] = set(ac.get("movie_logos", []))
            _ASSET_CACHE["movie_squares"] = set(ac.get("movie_squares", []))
            _ASSET_CACHE["series_logos"] = set(ac.get("series_logos", []))
            _ASSET_CACHE["series_squares"] = set(ac.get("series_squares", []))
            _ASSET_CACHE["art_scanned"] = bool(ac.get("art_scanned", False))
            _ASSET_CACHE["computed_at"] = ac["computed_at"]
            _rebuild_asset_index()
        _IGNORED.clear()
        _IGNORED.update(str(k) for k in payload.get("ignored_libraries", []))
        dash = payload.get("dashboard") or {}
        if dash.get("data") and dash.get("computed_at"):
            _DASHBOARD_CACHE["data"] = dash["data"]
            _DASHBOARD_CACHE["computed_at"] = dash["computed_at"]
    except Exception:
        pass  # missing or unreadable file: just start empty, as before


def compute_totals(library_stats, ignored=()):
    """Dashboard totals, summed over the libraries that are not ignored. Done on
    every page view (it is only a few additions), so ignoring or including a
    library takes effect at once, without a new check."""
    totals = {
        "total_items": 0,
        "poster_done": 0, "poster_total": 0,
        "bg_done": 0, "bg_total": 0,
        # season/episode coverage only exists for shows, tracked separately so
        # the percentage isn't diluted by movie libraries that have neither
        "season_done": 0, "season_total": 0,
        "episode_done": 0, "episode_total": 0,
        "logo_done": 0, "logo_total": 0,
        "square_done": 0, "square_total": 0,
    }
    for lib in library_stats:
        if str(lib["key"]) in ignored:
            continue
        total = lib["total"]
        totals["total_items"] += total
        totals["poster_done"] += lib["poster_done"]
        totals["poster_total"] += total
        totals["bg_done"] += lib["bg_done"]
        totals["bg_total"] += total
        totals["logo_done"] += lib.get("logo_done") or 0
        totals["logo_total"] += total
        totals["square_done"] += lib.get("square_done") or 0
        totals["square_total"] += total
        if lib.get("season_total") is not None:
            totals["season_done"] += lib["season_done"]
            totals["season_total"] += lib["season_total"]
            totals["episode_done"] += lib["episode_done"]
            totals["episode_total"] += lib["episode_total"]
    return totals


def compute_dashboard_stats(plex, asset_dir=None):
    sections = sorted(plex.library.sections(), key=lambda s: sort_key(s.title))
    library_stats = []
    # per library: [asset folder, seasons, episodes] of every item. It lets the
    # numbers be recomputed from the disk scan alone after a download, with no
    # call to Plex (see recompute_dashboard_counts)
    rows_by_section = {}

    for section in sections:
        if section.type not in ("movie", "show"):
            continue
        try:
            items = list(section.all())
        except requests.exceptions.RequestException:
            continue
        annotate_asset_flags(items, asset_dir)
        total = len(items)
        rows_by_section[str(section.key)] = [
            [_asset_name(i), getattr(i, "childCount", 0) or 0, getattr(i, "leafCount", 0) or 0]
            for i in items
        ]
        poster_done = sum(1 for i in items if i.has_poster)
        bg_done = sum(1 for i in items if i.has_background)
        logo_done = sum(1 for i in items if i.has_logo)
        square_done = sum(1 for i in items if i.has_square)

        lib_entry = {
            "key": section.key,
            "title": section.title,
            "type": section.type,
            "total": total,
            "poster_done": poster_done,
            "bg_done": bg_done,
            "logo_done": logo_done,
            "square_done": square_done,
        }

        if section.type == "show":
            # true counts, not "at least one per show": childCount is the
            # number of seasons (incl. Specials) and leafCount the number of
            # episodes, both already present on the Show object with no extra
            # Plex call needed
            season_total = sum(getattr(i, "childCount", 0) or 0 for i in items)
            season_done = sum(len(i.custom_seasons) for i in items)
            episode_total = sum(getattr(i, "leafCount", 0) or 0 for i in items)
            episode_done = sum(i.custom_episode_count for i in items)

            lib_entry["season_done"] = season_done
            lib_entry["season_total"] = season_total
            lib_entry["episode_done"] = episode_done
            lib_entry["episode_total"] = episode_total

        else:
            lib_entry["season_done"] = None
            lib_entry["season_total"] = None
            lib_entry["episode_done"] = None
            lib_entry["episode_total"] = None

        library_stats.append(lib_entry)

    library_stats.sort(key=lambda lib: (lib["type"], sort_key(lib["title"])))
    return {
        "library_stats": library_stats,
        "totals": compute_totals(library_stats),
        "rows": rows_by_section,
    }


def recompute_dashboard_counts():
    """Refresh the dashboard numbers (library bars, rings) from the cached disk
    scan alone — no call to Plex, so it is instant. The totals (how many movies,
    seasons, episodes exist) still come from the last full Update; this only
    redoes the "done" side. Returns False when there is nothing to recompute
    (no dashboard yet, or a state saved by an older version without the rows)."""
    data = _DASHBOARD_CACHE.get("data")
    if not data or not data.get("rows") or _ASSET_CACHE["computed_at"] is None:
        return False
    art_scanned = bool(_ASSET_CACHE.get("art_scanned"))
    season_episode = _ASSET_CACHE["series_season_episode"]
    for lib in data["library_stats"]:
        rows = data["rows"].get(str(lib["key"]))
        if rows is None:
            continue
        prefix = "series" if lib["type"] == "show" else "movie"
        posters = _ASSET_CACHE[f"{prefix}_posters"]
        backgrounds = _ASSET_CACHE[f"{prefix}_backgrounds"]
        logos = _ASSET_CACHE[f"{prefix}_logos"]
        squares = _ASSET_CACHE[f"{prefix}_squares"]
        lib["poster_done"] = sum(1 for r in rows if r[0] in posters)
        lib["bg_done"] = sum(1 for r in rows if r[0] in backgrounds)
        lib["logo_done"] = sum(1 for r in rows if r[0] in logos) if art_scanned else 0
        lib["square_done"] = sum(1 for r in rows if r[0] in squares) if art_scanned else 0
        if lib["type"] == "show":
            lib["season_done"] = sum(len(season_episode[r[0]]["seasons"]) for r in rows if r[0] in season_episode)
            lib["episode_done"] = sum(season_episode[r[0]]["episode_count"] for r in rows if r[0] in season_episode)
    data["totals"] = compute_totals(data["library_stats"])
    return True


def after_asset_write(item, base_dir):
    """A file was just written into base_dir. Bring the caches up to date the
    cheap way: re-read that one folder (a full scan only when nothing has been
    scanned yet), then redo the dashboard counts from memory. The old code
    walked the whole assets tree twice per download."""
    asset_root = os.path.join(os.getcwd(), session.get("asset_dir", "assets"))
    if _ASSET_CACHE["computed_at"] is None or not _ASSET_CACHE.get("art_scanned"):
        get_asset_folders(asset_root, force=True)  # saves the state itself
    else:
        kind = "movies" if item.type == "movie" else "series"
        update_asset_folder(base_dir, kind)
    recompute_dashboard_counts()
    save_state()


# ---- shared refresh + optional automatic refresh ---------------------------
_REFRESH_LOCK = threading.Lock()


def run_full_refresh(plex, asset_dir=None):
    """Rescan the asset folders on disk, then recompute the dashboard numbers.
    Used by the "Update" button and by the optional automatic refresh. Outside
    a request (background thread) the asset directory must be passed in."""
    asset_dir = asset_dir or session.get("asset_dir", "assets")
    with _REFRESH_LOCK:
        get_asset_folders(os.path.join(os.getcwd(), asset_dir), force=True)
        _DASHBOARD_CACHE["data"] = compute_dashboard_stats(plex, asset_dir)
        _DASHBOARD_CACHE["computed_at"] = time.time()
        save_state()


# AUTO_REFRESH_HOURS=6 recomputes everything every 6 hours, in the background.
# Empty / 0 = off (default). It needs PLEX_BASE_URL and PLEX_TOKEN in the
# environment (a background job has no browser session to borrow them from),
# and it never runs more often than every 15 minutes. The "Update" buttons keep
# working exactly as before; a manual update also postpones the next automatic one.
def _parse_hours(raw):
    try:
        hours = float((raw or "").strip().replace(",", "."))
    except ValueError:
        return 0.0
    return hours if hours > 0 else 0.0


AUTO_REFRESH_HOURS = _parse_hours(os.environ.get("AUTO_REFRESH_HOURS"))
AUTO_REFRESH_SECONDS = max(AUTO_REFRESH_HOURS * 3600, 15 * 60) if AUTO_REFRESH_HOURS else 0
_AUTO = {"active": False}


def _auto_refresh_tick(base_url, token, asset_dir, interval):
    """One scheduling step; returns the number of seconds to sleep afterwards."""
    wait = (_DASHBOARD_CACHE["computed_at"] or 0) + interval - time.time()
    if wait > 0:
        return min(wait, 60)  # not due yet (re-checked every minute at most)
    try:
        run_full_refresh(PlexServer(base_url, token, timeout=120), asset_dir)
    except Exception as e:
        print(f"[auto-refresh] failed, retrying in 15 minutes: {e}", flush=True)
        return 15 * 60
    return 60


def _auto_refresh_loop(base_url, token, asset_dir, interval):
    time.sleep(30)  # let Plex and the rest of the stack come up after a restart
    while True:
        time.sleep(_auto_refresh_tick(base_url, token, asset_dir, interval))


def start_auto_refresh():
    if not AUTO_REFRESH_SECONDS:
        return
    base_url = os.environ.get("PLEX_BASE_URL", "")
    token = os.environ.get("PLEX_TOKEN", "")
    if not (base_url and token):
        print("[auto-refresh] AUTO_REFRESH_HOURS is set but PLEX_BASE_URL / PLEX_TOKEN are not: "
              "automatic refresh is off.", flush=True)
        return
    asset_dir = os.environ.get("KOMETA_ASSET_DIR", "assets")
    _AUTO["active"] = True
    threading.Thread(
        target=_auto_refresh_loop,
        args=(base_url, token, asset_dir, AUTO_REFRESH_SECONDS),
        daemon=True,
        name="auto-refresh",
    ).start()


MEDIUX_TVDB_RE = re.compile(r"^\s{2}(\d+):\s*#\s*TVDB id for (.+?)\.\s*Set by", re.MULTILINE)


def extract_mediux_tvdb_ids(yml_text):
    """Pull {tvdb_id: show_name} straight out of a mediux-series.yml file via
    the '# TVDB id for <name>. Set by ...' comment on each entry — sidesteps
    needing a full YAML parse, which matters here since these files can have
    inconsistent indentation deeper in the season/episode blocks that a
    strict YAML parser chokes on (the top-level id lines themselves are
    reliably 2-space indented, so a full parse isn't needed anyway)."""
    return {int(tvdb_id): name.strip() for tvdb_id, name in MEDIUX_TVDB_RE.findall(yml_text)}


def parse_mediux_yaml(yml_text):
    """Parse the file with PyYAML. Returns (data, error, warnings).

    `error` is a human-readable message (with line/column) when the file isn't
    valid YAML, in which case data is None. `warnings` lists keys that appear
    twice at the same level: YAML silently keeps only the last one, so the
    earlier entry just vanishes without any error. PyYAML is imported lazily
    so that a missing install can never stop the whole app from starting."""
    try:
        import yaml
    except ImportError:
        return None, (
            "PyYAML is not installed in the container: add \"PyYAML\" to "
            "requirements.txt and rebuild. Until then, only the show-level "
            "comparison (without the season/episode detail) is available."
        ), []

    base = getattr(yaml, "CSafeLoader", yaml.SafeLoader)  # C parser when available: ~10x faster
    duplicates = []

    class DuplicateAwareLoader(base):
        pass

    def construct_mapping(loader, node, deep=False):
        seen = set()
        for key_node, _value_node in node.value:
            try:
                key = loader.construct_object(key_node, deep=True)
                if key in seen:
                    duplicates.append((key, key_node.start_mark.line + 1))
                seen.add(key)
            except Exception:
                pass
        return base.construct_mapping(loader, node, deep)

    DuplicateAwareLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, construct_mapping
    )

    try:
        data = yaml.load(yml_text, Loader=DuplicateAwareLoader)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        problem = getattr(e, "problem", None) or str(e)
        return None, f"YAML syntax error{where}: {problem}", []

    warnings = [
        f"Duplicate key \"{key}\" at line {line}: only the last occurrence is used."
        for key, line in duplicates
    ]
    return data, None, warnings


def collect_declared(data):
    """{tvdb_id: {season_number: {episode_numbers}}} as declared in the file."""
    declared = {}
    metadata = data.get("metadata") if isinstance(data, dict) else None
    if not isinstance(metadata, dict):
        return declared
    for key, entry in metadata.items():
        try:
            tvdb_id = int(key)
        except (TypeError, ValueError):
            continue
        seasons = {}
        raw_seasons = entry.get("seasons") if isinstance(entry, dict) else None
        if isinstance(raw_seasons, dict):
            for season_key, season_val in raw_seasons.items():
                try:
                    season_number = int(season_key)
                except (TypeError, ValueError):
                    continue
                episodes = set()
                raw_eps = season_val.get("episodes") if isinstance(season_val, dict) else None
                if isinstance(raw_eps, dict):
                    for ep_key in raw_eps:
                        try:
                            episodes.add(int(ep_key))
                        except (TypeError, ValueError):
                            pass
                seasons[season_number] = episodes
        declared[tvdb_id] = seasons
    return declared


def find_declared_gaps(declared_seasons, plex_seasons):
    """What the file declares that Plex doesn't have, the way Kometa reports it:
    a missing season is reported on its own (its episodes aren't listed again),
    and only seasons that DO exist get a per-episode check.
    Returns (missing_seasons, {season: missing_episodes})."""
    missing_seasons = sorted(s for s in declared_seasons if s not in plex_seasons)
    missing_episodes = {}
    for season, episodes in declared_seasons.items():
        if season in plex_seasons:
            gone = sorted(e for e in episodes if e not in plex_seasons[season])
            if gone:
                missing_episodes[season] = gone
    return missing_seasons, missing_episodes


def compress_ranges(numbers, fmt="{}"):
    """[13,14,15,18] -> 'E13–E15, E18' (with fmt='E{:02d}')."""
    nums = sorted(set(numbers))
    parts, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(fmt.format(nums[i]) if i == j else f"{fmt.format(nums[i])}–{fmt.format(nums[j])}")
        i = j + 1
    return ", ".join(parts)


# Reading every show's episode list from Plex is the only expensive part of the
# check (one request per show in the file). It only ever runs when the check
# button is pressed, requests go out a few at a time, and each show's structure
# is kept for a few minutes so fixing the file and re-checking is near-instant.
_PLEX_STRUCTURE_CACHE = {}  # rating_key -> {"at": timestamp, "seasons": {season: {episodes}}}
_PLEX_STRUCTURE_TTL = 600  # seconds
_PLEX_STRUCTURE_WORKERS = 4


def fetch_show_structure(plex, rating_key):
    """{season_number: {episode_numbers}} for one show, in a single request."""
    root = plex.query(f"/library/metadata/{rating_key}/allLeaves")
    seasons = {}
    if root is None:
        return seasons
    for video in root.iter("Video"):
        season, episode = video.get("parentIndex"), video.get("index")
        if season is None or episode is None:
            continue
        try:
            seasons.setdefault(int(season), set()).add(int(episode))
        except ValueError:
            continue
    return seasons


def load_show_structures(plex, shows, force=False):
    """-> (structures_by_rating_key, errors_by_rating_key, number_served_from_cache)"""
    now = time.time()
    structures, errors, hits, todo = {}, {}, 0, []
    for show in shows:
        rating_key = str(show.ratingKey)
        cached = _PLEX_STRUCTURE_CACHE.get(rating_key)
        if cached and not force and now - cached["at"] < _PLEX_STRUCTURE_TTL:
            structures[rating_key] = cached["seasons"]
            hits += 1
        else:
            todo.append(rating_key)
    if todo:
        with ThreadPoolExecutor(max_workers=_PLEX_STRUCTURE_WORKERS) as pool:
            futures = {rk: pool.submit(fetch_show_structure, plex, rk) for rk in todo}
            for rk, future in futures.items():
                try:
                    seasons = future.result()
                except Exception as e:
                    errors[rk] = f"{type(e).__name__}"
                    continue
                _PLEX_STRUCTURE_CACHE[rk] = {"at": time.time(), "seasons": seasons}
                structures[rk] = seasons
    return structures, errors, hits


# ---- Plex token: kept on the server, never in the cookie or in the pages ----
# The session cookie only carries a random connection id (or a "use the token
# from the environment" flag); the token itself lives in this in-memory table.
# Consequences: a restart of the container forgets manually typed tokens (the
# connection page asks again), and the app must run as ONE process (the other
# caches in this file already assume that).
_TOKENS = OrderedDict()  # connection id -> Plex token typed in by hand
_TOKENS_LOCK = threading.Lock()
MAX_TOKENS = 50


def get_token():
    if session.get("token_from_env"):
        return os.environ.get("PLEX_TOKEN") or None
    with _TOKENS_LOCK:
        return _TOKENS.get(session.get("cid"))


def forget_token():
    with _TOKENS_LOCK:
        _TOKENS.pop(session.get("cid"), None)
    session.pop("cid", None)
    session.pop("token_from_env", None)


def set_token(token, from_env=False):
    forget_token()
    session.pop("token", None)  # older versions kept the token in the cookie
    if from_env:
        session["token_from_env"] = True
        return
    cid = secrets.token_urlsafe(16)
    with _TOKENS_LOCK:
        _TOKENS[cid] = token
        while len(_TOKENS) > MAX_TOKENS:
            _TOKENS.popitem(last=False)
    session["cid"] = cid


def get_plex():
    base_url = session.get("base_url")
    token = get_token()
    if not base_url or not token:
        return None
    # default plexapi timeout is 30s; a big library or a busy Plex server
    # (e.g. mid-scan, or Kometa running) can take longer than that to answer
    # a full "section.all()" request, so give it more room before giving up
    return PlexServer(base_url, token, timeout=120)


@app.route("/", methods=["GET", "POST"])
def connect():
    # optional defaults from environment variables (set in docker-compose.yml)
    env_base_url = os.environ.get("PLEX_BASE_URL", "")
    env_token = os.environ.get("PLEX_TOKEN", "")
    env_asset_dir = os.environ.get("KOMETA_ASSET_DIR", "assets")

    if request.method == "POST":
        base_url = request.form["base_url"].strip()
        token = request.form.get("token", "").strip()
        # The configured token is never written into the page. A blank field
        # means "use the configured one" -- but only for the configured URL, so
        # the token can't be sent to a server the visitor typed in.
        use_env_token = False
        if not token and env_token and base_url.rstrip("/") == env_base_url.rstrip("/"):
            token, use_env_token = env_token, True
        if not token:
            return render_template(
                "connect.html",
                default_base_url=base_url,
                token_preset=bool(env_token),
                default_asset_dir=request.form.get("asset_dir", "") or env_asset_dir,
                error="A Plex token is required for this URL.",
            )
        session["base_url"] = base_url
        set_token(token, from_env=use_env_token)
        session["asset_dir"] = (
            "assets" if not request.form["asset_dir"] else request.form["asset_dir"]
        )
        return redirect(url_for("libraries"))

    # if both URL and token are provided via env, skip the form entirely
    if env_base_url and env_token and not get_token():
        session["base_url"] = env_base_url
        set_token(env_token, from_env=True)
        session["asset_dir"] = env_asset_dir
        return redirect(url_for("libraries"))

    return render_template(
        "connect.html",
        default_base_url=env_base_url,
        token_preset=bool(env_token),
        default_asset_dir=env_asset_dir,
        error=None,
    )


# Images hosted by Plex are fetched by the server, so the browser never sees
# the token (and never needs to reach the Plex address itself).
_PLEX_IMG_RE = re.compile(r"^/library/[A-Za-z0-9_\-./:%?&=+]+$")


@app.route("/plex_img")
def plex_img():
    base_url = session.get("base_url")
    token = get_token()
    if not base_url or not token:
        abort(403)
    path = request.args.get("path", "")
    if not _PLEX_IMG_RE.match(path) or ".." in path:
        abort(400)
    try:
        r = requests.get(
            base_url.rstrip("/") + path,
            headers={"X-Plex-Token": token},
            timeout=30,
        )
    except requests.exceptions.RequestException:
        abort(502)
    if r.status_code != 200:
        abort(502)
    content_type = r.headers.get("Content-Type", "")
    if not content_type.startswith("image/"):
        abort(502)
    resp = Response(r.content, content_type=content_type)
    # Plex puts a timestamp in the path, so a changed image gets a new URL
    resp.headers["Cache-Control"] = "private, max-age=86400"
    return resp


@app.route("/libraries")
def libraries():
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))

    if _DASHBOARD_CACHE["data"] is None or request.args.get("refresh") == "1":
        run_full_refresh(plex)

    library_stats = _DASHBOARD_CACHE["data"]["library_stats"]

    computed_at_display = None
    if _DASHBOARD_CACHE["computed_at"]:
        computed_at_display = datetime.datetime.fromtimestamp(
            _DASHBOARD_CACHE["computed_at"]
        ).strftime("%d/%m/%Y at %H:%M")

    return render_template(
        "libraries.html",
        library_stats=library_stats,
        totals=compute_totals(library_stats, _IGNORED),
        ignored_keys=_IGNORED,
        ignored_count=sum(1 for lib in library_stats if str(lib["key"]) in _IGNORED),
        computed_at=_DASHBOARD_CACHE["computed_at"],
        computed_at_display=computed_at_display,
        auto_refresh_hours=("%g" % (AUTO_REFRESH_SECONDS / 3600)) if _AUTO["active"] else None,
    )


@app.route("/dashboard/ignore/<section_key>", methods=["POST"])
def toggle_ignore_library(section_key):
    """Leave a library out of (or back into) the dashboard totals."""
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))
    key = str(section_key)
    known = {str(lib["key"]) for lib in (_DASHBOARD_CACHE["data"] or {}).get("library_stats", [])}
    if key in known:
        if key in _IGNORED:
            _IGNORED.discard(key)
        else:
            _IGNORED.add(key)
        save_state()
    return redirect(url_for("libraries"))


# which dashboard numbers back each ring: (done key, total key) in library_stats
MISSING_RING_FIELDS = {
    "poster": ("poster_done", "total"),
    "background": ("bg_done", "total"),
    "logo": ("logo_done", "total"),
    "square": ("square_done", "total"),
    "season": ("season_done", "season_total"),
    "episode": ("episode_done", "episode_total"),
}
MISSING_UNITS = {
    "poster": "posters",
    "background": "backgrounds",
    "logo": "logos",
    "square": "square",
    "season": "seasons",
    "episode": "episodes",
}


@app.route("/missing/<missing_type>")
def missing_overview(missing_type):
    """Target of a dashboard ring click. One library concerned: jump straight to
    its filtered list. Several: let the user pick which one."""
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))
    if missing_type not in MISSING_RING_FIELDS:
        return redirect(url_for("libraries"))
    data = _DASHBOARD_CACHE["data"]
    if data is None:
        return redirect(url_for("libraries"))

    done_key, total_key = MISSING_RING_FIELDS[missing_type]
    libs = []
    for lib in data["library_stats"]:
        if str(lib["key"]) in _IGNORED:
            continue  # left out of the totals, so out of the rings' picker too
        done, total = lib.get(done_key), lib.get(total_key)
        if done is None or total is None:
            continue  # e.g. season/episode figures don't exist for movie libraries
        if total - done > 0:
            libs.append({**lib, "missing": total - done, "of": total})

    if not libs:
        flash("Nothing missing here — everything is covered.")
        return redirect(url_for("libraries"))
    if len(libs) == 1:
        return redirect(url_for("list_items", section_key=libs[0]["key"], missing_type=missing_type))
    return render_template(
        "missing_overview.html",
        libs=libs,
        missing_type=missing_type,
        unit=MISSING_UNITS[missing_type],
    )


# ---- export of what is missing (CSV / text) ---------------------------------
EXPORT_SECTION_TYPES = ("movie", "show")
EXPORT_MISSING_NAMES = {
    "poster": "poster",
    "background": "background",
    "logo": "logo",
    "square": "square",
    "season": "seasons",
    "episode": "episodes",
}


def _export_kinds(section_type, art_types):
    kinds = ["poster", "background"] + list(art_types)
    if section_type == "show":
        kinds += ["season", "episode"]
    return kinds


def _export_columns(art_types):
    columns = ["Library", "Title", "Year", "Type", "Missing", "Poster", "Background"]
    if "logo" in art_types:
        columns.append("Logo")
    if "square" in art_types:
        columns.append("Square")
    return columns + ["Seasons", "Episodes", "TMDB", "TVDB", "IMDB", "Plex key", "Asset folder"]


def _csv_cell(value):
    """Plain text for a spreadsheet cell; a leading = + - @ would be read as a
    formula by Excel/LibreOffice, so it is neutralised."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    return text


def _item_external_ids(item):
    ids = {}
    try:
        for guid in item.guids:
            kind, _, value = (guid.id or "").partition("://")
            if kind in ("tmdb", "tvdb", "imdb") and value:
                ids[kind] = value
    except Exception:
        pass
    return ids


def _asset_folder_name(item):
    return _asset_name(item) or ""


def collect_missing_rows(section, missing_type="", art_types=()):
    """One row per item of this library that lacks something. With
    missing_type, only the items lacking that one thing. Needs a check to have
    been run (annotate_asset_flags reads the last check). art_types: the
    optional kinds (logo / square) to include."""
    kinds = _export_kinds(section.type, art_types)
    if missing_type and missing_type not in kinds:
        return []
    items = sort_items(list(section.all()), "title")
    annotate_asset_flags(items)
    rows = []
    for item in items:
        missing = [t for t in kinds if _is_missing(item, t)]
        if not missing or (missing_type and missing_type not in missing):
            continue
        ids = _item_external_ids(item)
        is_show = section.type == "show"
        rows.append({
            "Library": section.title,
            "Title": item.title,
            "Year": getattr(item, "year", "") or "",
            "Type": section.type,
            "Missing": "; ".join(EXPORT_MISSING_NAMES[t] for t in missing),
            "Poster": "yes" if item.has_poster else "no",
            "Background": "yes" if item.has_background else "no",
            "Logo": "yes" if item.has_logo else "no",
            "Square": "yes" if item.has_square else "no",
            "Seasons": f"{len(item.custom_seasons)}/{getattr(item, 'childCount', 0) or 0}" if is_show else "",
            "Episodes": f"{item.custom_episode_count}/{getattr(item, 'leafCount', 0) or 0}" if is_show else "",
            "TMDB": ids.get("tmdb", ""),
            "TVDB": ids.get("tvdb", ""),
            "IMDB": ids.get("imdb", ""),
            "Plex key": item.ratingKey,
            "Asset folder": _asset_folder_name(item),
        })
    return rows


def _rows_to_csv(rows, art_types=()):
    columns = _export_columns(art_types)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(row[c]) for c in columns])
    return "\ufeff" + buf.getvalue()  # BOM: Excel then reads the accents correctly


def _rows_to_text(rows):
    lines = []
    libraries = []
    for row in rows:
        if row["Library"] not in libraries:
            libraries.append(row["Library"])
    for lib in libraries:
        if len(libraries) > 1:
            lines += ([""] if lines else []) + [f"# {lib}"]
        for row in rows:
            if row["Library"] != lib:
                continue
            year = f" ({row['Year']})" if row["Year"] else ""
            lines.append(f"{row['Title']}{year} \u2014 {row['Missing']}")
    return "\n".join(lines) + "\n"


@app.route("/export")
def export_missing():
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))

    section_key = request.args.get("section_key", "")
    missing_type = request.args.get("missing_type", "")
    if missing_type not in MISSING_TYPES:
        missing_type = ""
    fmt = "txt" if request.args.get("format") == "txt" else "csv"
    if section_key:
        back = url_for("list_items", section_key=section_key, missing_type=missing_type or None)
    else:
        back = url_for("libraries")

    if _ASSET_CACHE["computed_at"] is None:
        flash("Run a check first (\U0001F504 Update) before exporting.")
        return redirect(back)

    art_types = active_art_types()
    try:
        sections = [s for s in plex.library.sections() if s.type in EXPORT_SECTION_TYPES]
        sections.sort(key=lambda s: (s.type, sort_key(s.title)))
        if section_key:
            sections = [s for s in sections if str(s.key) == section_key]
            if not sections:
                flash("Library not found.")
                return redirect(url_for("libraries"))
        else:
            # the dashboard export follows the dashboard totals: ignored libraries stay out
            sections = [s for s in sections if str(s.key) not in _IGNORED]
        rows = []
        for s in sections:
            rows += collect_missing_rows(s, missing_type, art_types)
    except requests.exceptions.RequestException:
        flash("Plex took too long to respond (busy server or large library). Try again in a moment.")
        return redirect(back)

    if not rows:
        flash("Nothing missing \u2014 nothing to export.")
        return redirect(back)

    scope = re.sub(r"[^A-Za-z0-9]+", "-", sections[0].title).strip("-").lower() if section_key else "all"
    name = f"artlayerr-missing-{scope or 'library'}-{datetime.date.today().isoformat()}.{fmt}"
    body = _rows_to_csv(rows, art_types) if fmt == "csv" else _rows_to_text(rows)
    resp = Response(body, mimetype="text/csv" if fmt == "csv" else "text/plain")
    resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


@app.route("/mediux_check", methods=["GET", "POST"])
def mediux_check():
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))

    results = None

    if request.method == "POST":
        uploads = [f for f in request.files.getlist("yml_file") if f and f.filename]
        if not uploads:
            flash("No files selected.")
            return redirect(url_for("mediux_check"))

        started = time.time()

        # One entry per uploaded file. Names come from the "# TVDB id for <name>"
        # comments (YAML parsing drops comments); ids also come from the parsed
        # file when it is valid YAML.
        parsed = []
        for upload in uploads:
            try:
                yml_text = upload.read().decode("utf-8", errors="replace")
            except Exception as e:
                flash(f"Could not read \"{upload.filename}\": {e}")
                continue
            names = extract_mediux_tvdb_ids(yml_text)
            data, error, warnings = parse_mediux_yaml(yml_text)
            declared = collect_declared(data) if error is None else {}
            parsed.append({
                "name": upload.filename,
                "names": names,
                "declared": declared,
                "ids": set(names) | set(declared),
                "yaml_error": error,
                "yaml_warnings": warnings,
                "buckets": {},
                "gap_totals": {"shows": 0, "seasons": 0, "episodes": 0},
            })
        if not parsed:
            return redirect(url_for("mediux_check"))

        # a Plex show counts as covered if ANY of the files has it
        all_ids = set().union(*(pf["ids"] for pf in parsed))
        all_names = {}
        for pf in parsed:
            for tvdb_id, name in pf["names"].items():
                all_names.setdefault(tvdb_id, name)

        show_sections = [s for s in plex.library.sections() if s.type == "show"]

        missing_by_library = []
        candidates = []  # (file, section, show, tvdb_id): a show that is in a given file
        plex_tvdb_ids = set()
        total_shows = 0
        total_missing = 0

        for section in show_sections:
            try:
                shows = list(section.all())
            except requests.exceptions.RequestException:
                continue
            missing_here = []
            for idx, show in enumerate(shows, start=1):
                tvdb_id = None
                try:
                    for guid in show.guids:
                        if guid.id.startswith("tvdb://"):
                            tvdb_id = int(guid.id.replace("tvdb://", ""))
                            break
                except Exception:
                    pass
                show.browse_index = idx
                if tvdb_id is not None:
                    plex_tvdb_ids.add(tvdb_id)
                    for pf in parsed:
                        if tvdb_id in pf["declared"]:
                            candidates.append((pf, section, show, tvdb_id))
                if tvdb_id is None or tvdb_id not in all_ids:
                    missing_here.append(show)
            total_shows += len(shows)
            total_missing += len(missing_here)
            if missing_here:
                missing_by_library.append({
                    "section_title": section.title,
                    "section_key": section.key,
                    "shows": sorted(missing_here, key=lambda s: sort_key(s.title)),
                })

        # seasons/episodes each file declares but Plex doesn't have
        unreadable = []
        gap_show_keys = set()
        shows_read, from_cache = 0, 0
        if candidates:
            # a show listed in several files is only read from Plex once
            unique_shows = {str(show.ratingKey): show for _pf, _sec, show, _tvdb in candidates}
            structures, errors, from_cache = load_show_structures(
                plex,
                list(unique_shows.values()),
                force=request.form.get("force_refresh") == "1",
            )
            shows_read = len(unique_shows)
            unreadable_seen = set()
            for pf, section, show, tvdb_id in candidates:
                rating_key = str(show.ratingKey)
                if rating_key in errors:
                    if rating_key not in unreadable_seen:
                        unreadable_seen.add(rating_key)
                        unreadable.append({"title": show.title, "library": section.title})
                    continue
                miss_seasons, miss_episodes = find_declared_gaps(
                    pf["declared"][tvdb_id], structures.get(rating_key, {})
                )
                if not miss_seasons and not miss_episodes:
                    continue
                bucket = pf["buckets"].setdefault(section.key, {
                    "section_title": section.title,
                    "section_key": section.key,
                    "items": [],
                })
                bucket["items"].append({
                    "title": show.title,
                    "tvdb_id": tvdb_id,
                    "browse_index": show.browse_index,
                    "missing_seasons": ", ".join(f"S{n:02d}" for n in miss_seasons),
                    "missing_episodes": [
                        {"season": f"S{n:02d}", "episodes": compress_ranges(eps, "E{:02d}")}
                        for n, eps in sorted(miss_episodes.items())
                    ],
                })
                pf["gap_totals"]["shows"] += 1
                pf["gap_totals"]["seasons"] += len(miss_seasons)
                pf["gap_totals"]["episodes"] += sum(len(v) for v in miss_episodes.values())
                gap_show_keys.add(rating_key)

        files_out = []
        for pf in parsed:
            for bucket in pf["buckets"].values():
                bucket["items"].sort(key=lambda e: sort_key(e["title"]))
            files_out.append({
                "name": pf["name"],
                "entry_count": len(pf["ids"]),
                "yaml_error": pf["yaml_error"],
                "yaml_warnings": pf["yaml_warnings"],
                "gaps_by_library": sorted(
                    pf["buckets"].values(), key=lambda b: sort_key(b["section_title"])
                ),
                "gap_totals": pf["gap_totals"],
                "not_in_plex": sorted(
                    (
                        {"tvdb_id": i, "name": pf["names"].get(i) or all_names.get(i, "?")}
                        for i in pf["ids"] if i not in plex_tvdb_ids
                    ),
                    key=lambda e: sort_key(e["name"]),
                ),
            })

        results = {
            "files": files_out,
            "multi": len(files_out) > 1,
            "yml_entry_count": len(all_ids),
            "total_shows": total_shows,
            "total_missing": total_missing,
            "missing_by_library": missing_by_library,
            "gap_show_count": len(gap_show_keys),
            "unreadable": unreadable,
            "shows_read": shows_read,
            "from_cache": from_cache,
            "elapsed": round(time.time() - started, 1),
        }

    return render_template("mediux_check.html", results=results)


# new paginated item picker
@app.route("/browse/<section_key>/items")
def list_items(section_key):
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))

    # find the right library section
    section = next(
        (s for s in plex.library.sections() if str(s.key) == section_key),
        None,
    )
    if not section:
        flash("Library not found.")
        return redirect(url_for("libraries"))

    # full list of items, in the chosen sort order — the item page's
    # Previous/Next use the same order (same cookie), so positions line up
    sort = get_sort_pref()
    try:
        all_items_full = sort_items(list(section.all()), sort)
    except requests.exceptions.RequestException:
        flash("Plex took too long to respond (busy server or large library). Try again in a moment.")
        return redirect(url_for("libraries"))

    # give everything a global index
    global_index = 1
    for item in all_items_full:
        item.global_index = global_index
        global_index = global_index + 1

    # check which items already have a downloaded poster / background —
    # purely from the last manual check, never scans the disk itself
    annotate_asset_flags(all_items_full)
    checked = _ASSET_CACHE["computed_at"] is not None

    # which filter buttons make sense for this library type
    available_missing_types = missing_types_for(section.type)

    # requested filter (missing or has_), validated against what's actually offered here
    missing_type = request.args.get("missing_type", "")
    if missing_type not in filter_types_for(section.type):
        missing_type = ""
    # meaningless before a check has ever been run, so it's simply ignored
    if not checked:
        missing_type = ""

    # separate index used when navigating within a "missing X only" set, so
    # Next/Previous on the item page stay within that filtered subset instead
    # of jumping around based on the full library's positions
    missing_index = 1
    if missing_type:
        for item in all_items_full:
            if _matches(item, missing_type):
                item.missing_index = missing_index
                missing_index += 1
            else:
                item.missing_index = None
    else:
        for item in all_items_full:
            item.missing_index = None

    # optional free-text search — takes priority over the alphabet filter
    search_query = request.args.get("q", "").strip()
    letter = request.args.get("letter", "All")
    if search_query:
        letter = "All"
        items_filtered = [
            item
            for item in all_items_full
            if item.title and search_query.lower() in item.title.lower()
        ]
    elif letter != "All":
        if letter[:1].isdigit():
            items_filtered = [
                item
                for item in all_items_full
                if item.title and item.title[:1].isdigit()
            ]
        else:
            items_filtered = [
                item
                for item in all_items_full
                if item.title and item.title.upper().startswith(letter.upper())
            ]
    else:
        items_filtered = all_items_full

    if missing_type:
        items_filtered = [item for item in items_filtered if _matches(item, missing_type)]

    # pagination params (based on filtered set)
    total    = len(items_filtered)
    per_page = get_per_page_pref()
    pages    = math.ceil(total / per_page) if total else 1

    # clamp page number
    page = int(request.args.get("page", 1))
    page = max(1, min(page, pages))

    # slice out this page from filtered items
    start      = (page - 1) * per_page
    page_items = items_filtered[start : start + per_page]
    annotate_poster_source(page_items)

    missing_counts = {
        t: (len([i for i in all_items_full if _is_missing(i, t)]) if checked else 0)
        for t in available_missing_types
    }
    has_counts = {
        t: (len([i for i in all_items_full if _has(i, t)]) if checked else 0)
        for t in available_missing_types
    }

    resp = make_response(render_template(
        "items.html",
        section=section,
        checked=checked,
        items=page_items,
        page=page,
        pages=pages,
        per_page=per_page,
        per_page_options=PER_PAGE_OPTIONS,
        sort=sort,
        sort_options=SORT_OPTIONS,
        alphabet=ALPHABET,
        letter=letter,
        search_query=search_query,
        missing_type=missing_type,
        missing_counts=missing_counts,
        has_counts=has_counts,
        available_missing_types=available_missing_types,
        last_check_ts=_ASSET_CACHE["computed_at"] if checked else None,
        last_check_display=(
            datetime.datetime.fromtimestamp(_ASSET_CACHE["computed_at"]).strftime("%d/%m/%Y at %H:%M")
            if checked else None
        ),
    ))
    # remember the choices for next time and for the item page's navigation
    resp.set_cookie("artlayerr_sort", sort, max_age=PREF_COOKIE_MAX_AGE, samesite="Lax")
    resp.set_cookie("artlayerr_per_page", str(per_page), max_age=PREF_COOKIE_MAX_AGE, samesite="Lax")
    return resp


@app.route("/refresh_assets/<section_key>")
def refresh_assets(section_key):
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))

    refresh_asset_check(plex, section_key)
    flash("Posters/backgrounds check completed.")

    # bounce back to wherever the person was (list or item page), keeping
    # whatever letter/search/page/missing_type state was already in the URL
    return_to = request.args.get("return_to", "list")
    if return_to == "item":
        return redirect(
            url_for(
                "browse",
                section_key=section_key,
                page=request.args.get("page", 1),
                letter=request.args.get("letter", "All"),
                art_type=request.args.get("art_type", "poster"),
                season=request.args.get("season"),
                episode=request.args.get("episode"),
                art_page=request.args.get("art_page", 1),
                missing_type=request.args.get("missing_type"),
            )
        )
    return redirect(
        url_for(
            "list_items",
            section_key=section_key,
            page=request.args.get("page", 1),
            letter=request.args.get("letter", "All"),
            q=request.args.get("q") or None,
            missing_type=request.args.get("missing_type"),
        )
    )


# ---- where an image lives: on Plex, in the assets folder, or both ----------
# The assets folder is what Kometa reads; Plex only shows an image once Kometa
# has applied it. Plex writes the time a picture last changed at the end of its
# path (/library/metadata/123/thumb/1699999999), so comparing that with the
# file's date tells whether Plex was updated after the file was written. It is
# an estimate (Plex also changes that date when it refreshes metadata), which
# is why the wording in the pages is careful.
_PLEX_TS_RE = re.compile(r"/(\d{9,})(?:\?.*)?$")
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def plex_art_timestamp(path):
    m = _PLEX_TS_RE.search(path or "")
    return int(m.group(1)) if m else None


def _top_folder(asset_root, path):
    rel = os.path.relpath(path, asset_root)
    return rel.split(os.sep)[0] if rel and rel != "." else ""


def _find_dir_ci(parent, wanted):
    """Subfolder of `parent` whose name equals `wanted`, ignoring case."""
    try:
        with os.scandir(parent) as it:
            for e in it:
                if e.is_dir() and e.name.lower() == (wanted or "").lower():
                    return e.path
    except OSError:
        pass
    return None


def _asset_folder_for(item, asset_root, for_write=False):
    """Folder holding the assets of this item, wherever it lives under the assets
    root. For an item without any folder yet: the legacy movies/series path when
    reading, and, when writing, the library's own folder (assets/<Library>/) if
    the assets are organised that way, else assets/movies | assets/series."""
    name = _asset_name(item)
    if not name and for_write:
        name = item.title
    if not name:
        return None
    kind_dir = "movies" if item.type == "movie" else "series"
    legacy_parent = os.path.join(asset_root, kind_dir)
    library = getattr(item, "librarySectionTitle", None) or ""
    found = _dir_paths(asset_root, name)
    if found:
        def rank(p):
            top = _top_folder(asset_root, p)
            return (top.lower() != library.lower(), top != kind_dir)
        return sorted(found, key=rank)[0]
    if not for_write:
        return os.path.join(legacy_parent, name)
    if os.path.isdir(legacy_parent):
        parent = legacy_parent
    else:
        parent = _find_dir_ci(asset_root, library) or legacy_parent
    return os.path.join(parent, name)


def newest_asset_file(folder, match):
    """(filename, mtime) of the newest image in one folder whose lower-cased name
    satisfies match(name); None when there is none. One directory listing."""
    best = None
    try:
        with os.scandir(folder) as files:
            for f in files:
                low = f.name.lower()
                if not f.is_file() or not low.endswith(_IMG_EXTS) or not match(low):
                    continue
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                if best is None or mtime > best[1]:
                    best = (f.name, mtime)
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        pass
    return best


def annotate_poster_source(items):
    """Set item.poster_src ('plex' / 'assets' / 'both', None when not checked)
    and a tooltip for the items of ONE page of the grid. Only items that have a
    poster in assets cost a directory listing."""
    asset_root = os.path.join(os.getcwd(), session.get("asset_dir", "assets"))
    for item in items:
        item.poster_src = None
        item.poster_src_tip = ""
        has = getattr(item, "has_poster", None)
        if has is None:
            continue
        if not has:
            item.poster_src = "plex"
            item.poster_src_tip = "Poster: only on Plex, nothing in the assets folder yet"
            continue
        folder = _asset_folder_for(item, asset_root)
        found = newest_asset_file(folder, lambda n: n.startswith("poster")) if folder else None
        ts = plex_art_timestamp(getattr(item, "thumb", None))
        if found and ts is not None and ts >= found[1]:
            item.poster_src = "both"
            item.poster_src_tip = "Poster: in the assets folder and Plex was updated after it (estimated: Kometa has applied it)"
        else:
            item.poster_src = "assets"
            item.poster_src_tip = (
                "Poster: in the assets folder, not applied on Plex yet (Kometa has not run since)"
                if found and ts is not None
                else "Poster: in the assets folder (cannot tell if Plex shows it)"
            )


def build_art_panel(item, source, art_type, season, episode):
    """What the assets folder holds for the image being browsed (show / season /
    episode level), for the panel above the candidates on the item page."""
    asset_root = os.path.join(os.getcwd(), session.get("asset_dir", "assets"))
    folder = _asset_folder_for(item, asset_root)
    if not folder:
        return None
    try:
        stem = os.path.splitext(compute_asset_filename(item, art_type, season, episode, ".x"))[0].lower()
    except (TypeError, ValueError):
        return None
    stems = {stem, "clearlogo"} if stem == "logo" else {stem}
    found = newest_asset_file(folder, lambda n: os.path.splitext(n)[0] in stems)
    panel = {"file": None, "url": None, "when": None, "state": "plex"}
    if not found:
        return panel
    name, mtime = found
    panel["file"] = name
    panel["when"] = datetime.datetime.fromtimestamp(mtime).strftime("%d/%m/%Y %H:%M")
    panel["url"] = url_for(
        "asset_img",
        kind="movies" if item.type == "movie" else "series",
        folder=os.path.basename(folder),
        file=name,
        v=int(mtime),
    )
    # Plex only dates posters and backgrounds; logos / square art cannot be compared
    attr = {"poster": "thumb", "background": "art"}.get(art_type)
    ts = plex_art_timestamp(getattr(source, attr, None)) if attr else None
    if ts is None:
        panel["state"] = "unknown"
    else:
        panel["state"] = "both" if ts >= mtime else "assets"
    return panel


@app.route("/asset_img")
def asset_img():
    """Serve one image of the assets folder (the panel on the item page)."""
    if not session.get("base_url") or not get_token():
        abort(403)
    kind = request.args.get("kind", "")
    folder = request.args.get("folder", "")
    filename = request.args.get("file", "")
    if kind not in ("movies", "series") or not filename.lower().endswith(_IMG_EXTS):
        abort(400)
    for part in (folder, filename):
        if not part or part in (".", "..") or "/" in part or "\\" in part or "\x00" in part:
            abort(400)
    asset_root = os.path.join(os.getcwd(), session.get("asset_dir", "assets"))
    root = os.path.realpath(asset_root)
    path = None
    for candidate in _dir_paths(asset_root, folder):
        p = os.path.realpath(os.path.join(candidate, filename))
        if os.path.commonpath([root, p]) == root and os.path.isfile(p):
            path = p
            break
    if path is None:
        abort(404)
    resp = send_file(path, conditional=True)
    resp.headers["Cache-Control"] = "private, max-age=86400"  # the URL carries the file date (v=)
    return resp


# Which levels of ONE show already have a file in the assets folder, for the
# art type currently being browsed. It's a single directory listing of that one
# show's folder (not the whole library), so it costs next to nothing per page.
_LOCAL_EPISODE_FILE_RE = re.compile(r"^s(\d+)e(\d+)(_background)?\.", re.IGNORECASE)
_LOCAL_SEASON_FILE_RE = re.compile(r"^season(\d+)(_background)?\.", re.IGNORECASE)
_LOCAL_SHOW_FILE_RE = re.compile(r"^(poster|background)\.", re.IGNORECASE)


def scan_show_local_files(item, art_type):
    result = {"show_level": False, "seasons": set(), "episodes": set()}
    if getattr(item, "type", None) != "show":
        return result
    want_background = art_type == "background"
    try:
        item_dir = _asset_folder_for(
            item, os.path.join(os.getcwd(), session.get("asset_dir", "assets"))
        )
        if not item_dir:
            return result
        with os.scandir(item_dir) as entries:
            for entry in entries:
                if not entry.is_file():
                    continue
                m = _LOCAL_EPISODE_FILE_RE.match(entry.name)
                if m:
                    if bool(m.group(3)) == want_background:
                        result["episodes"].add((int(m.group(1)), int(m.group(2))))
                    continue
                m = _LOCAL_SEASON_FILE_RE.match(entry.name)
                if m:
                    if bool(m.group(2)) == want_background:
                        result["seasons"].add(int(m.group(1)))
                    continue
                m = _LOCAL_SHOW_FILE_RE.match(entry.name)
                if m and (m.group(1).lower() == "background") == want_background:
                    result["show_level"] = True
    except (OSError, IndexError):
        pass  # no folder yet for this show, or no location: nothing to mark
    return result


@app.route("/browse/<section_key>")
def browse(section_key):
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))
    section = next(
        (s for s in plex.library.sections() if str(s.key) == section_key), None
    )
    if not section:
        flash("Library not found.")
        return redirect(url_for("libraries"))
    missing_type = request.args.get("missing_type", "")
    if missing_type not in filter_types_for(section.type):
        missing_type = ""

    # The grid links carry the clicked item's ratingKey (rk) next to its position
    # (page). The position is only a hint: it counts positions in a list that was
    # built earlier, and a title added to or removed from Plex since then shifts
    # every position after it. rk always wins.
    wanted = request.args.get("rk", type=int)

    if missing_type:
        try:
            missing_keys = get_missing_rating_keys(
                section, missing_type=missing_type, sort=get_sort_pref()
            )
            if missing_keys and wanted is not None and wanted not in missing_keys:
                # the cached list predates a change in the library: rebuild it once
                missing_keys = get_missing_rating_keys(
                    section, missing_type=missing_type, sort=get_sort_pref(), force=True
                )
        except requests.exceptions.RequestException:
            flash("Plex took too long to respond (busy server or large library). Try again in a moment.")
            return redirect(url_for("libraries"))
        if missing_keys is None:
            flash("Run a check first before using this filter.")
            return redirect(url_for("list_items", section_key=section.key))
        pages = len(missing_keys)
        if not pages:
            label = MISSING_TYPES[filter_base(missing_type)]["label"]
            if missing_type.startswith(HAS_PREFIX):
                flash(f"No item with {label} \u2014 filter removed.")
            else:
                flash(f"No {label} missing \u2014 filter removed.")
            return redirect(url_for("list_items", section_key=section.key))
        item_page = int(request.args.get("page", 1))
        item_letter = request.args.get("letter", "All")
        item_page = max(1, min(item_page, pages))
        if wanted is not None and wanted in missing_keys:
            item_page = missing_keys.index(wanted) + 1
        item = None
        if wanted is not None:
            try:
                item = plex.fetchItem(wanted)  # still opens the clicked title if it just left the list
            except Exception:
                item = None
        if item is None:
            item = plex.fetchItem(missing_keys[item_page - 1])
    else:
        try:
            items = sort_items(list(section.all()), get_sort_pref())
        except requests.exceptions.RequestException:
            flash("Plex took too long to respond (busy server or large library). Try again in a moment.")
            return redirect(url_for("libraries"))
        pages = len(items)
        item_page = int(request.args.get("page", 1))
        item_letter = request.args.get("letter", "All")
        item_page = max(1, min(item_page, pages))
        if wanted is not None:
            item_page = next((n for n, it in enumerate(items, 1) if it.ratingKey == wanted), item_page)
        item = items[item_page - 1]

    # same badges shown on the items.html grid, for a quick reminder here too
    annotate_asset_flags([item])

    art_type = request.args.get("art_type", "poster")
    if art_type not in ART_TYPES:
        art_type = "poster"
    season = request.args.get("season") or None
    episode = request.args.get("episode") or None
    if art_type in ART_EXTRAS:  # logos / square art only exist at movie / show level
        season = None
        episode = None
    art_page = int(request.args.get("art_page", 1))

    # Seasons/episodes are fetched ONCE here and handed to the template, which
    # used to re-query Plex for them five to ten times per page view.
    show_seasons, selected_season, season_episodes, selected_episode = [], None, [], None
    season_rating_key = None
    if section.type == "show":
        show_seasons = item.seasons()
        if season:
            try:
                season_number = int(season)
            except ValueError:
                season_number = None
            selected_season = next(
                (s for s in show_seasons if s.seasonNumber == season_number), None
            )
        if selected_season is not None:
            season_rating_key = selected_season.ratingKey
            season_episodes = selected_season.episodes()
            if episode == "first":  # set by the "Episodes" tab, resolved only on click
                episode = str(season_episodes[0].index) if season_episodes else None
            if episode:
                try:
                    episode_number = int(episode)
                except ValueError:
                    episode_number = None
                selected_episode = next(
                    (e for e in season_episodes if e.index == episode_number), None
                )
        # an unknown season/episode in the URL falls back to the level above
        # instead of leaving the page half show-level, half season-level
        if selected_season is None:
            season = None
            episode = None
        elif selected_episode is None:
            episode = None
    else:
        season = None
        episode = None

    local_files = scan_show_local_files(item, art_type)

    # the item the candidates below belong to (same choice as item.html makes)
    if section.type == "movie":
        art_source = item
    elif selected_episode:
        art_source = selected_episode
    elif selected_season:
        art_source = selected_season
    else:
        art_source = item
    art_panel = build_art_panel(item, art_source, art_type, season, episode)

    # direct link to this item's page in the Plex web app
    try:
        plex_web_url = (
            f"{session['base_url']}/web/index.html#!/server/"
            f"{plex.machineIdentifier}/details?key=%2Flibrary%2Fmetadata%2F{item.ratingKey}"
        )
    except Exception:
        plex_web_url = None

    # External links from the Plex guids: IMDb for everything,
    # TVDB for shows, TMDB for movies, Mediux (keyed by TMDB id) for both
    imdb_url = None
    tvdb_url = None
    tmdb_url = None
    mediux_url = None
    try:
        for guid in item.guids:
            if guid.id.startswith("imdb://") and imdb_url is None:
                imdb_url = f"https://www.imdb.com/title/{guid.id.replace('imdb://', '')}/"
            elif item.type == "show" and guid.id.startswith("tvdb://") and tvdb_url is None:
                tvdb_url = f"https://thetvdb.com/dereferrer/series/{guid.id.replace('tvdb://', '')}"
            elif item.type == "movie" and guid.id.startswith("tmdb://") and tmdb_url is None:
                tmdb_url = f"https://www.themoviedb.org/movie/{guid.id.replace('tmdb://', '')}"
            if guid.id.startswith("tmdb://") and mediux_url is None:
                mediux_kind = "movies" if item.type == "movie" else "shows"
                mediux_url = f"https://mediux.pro/{mediux_kind}/{guid.id.replace('tmdb://', '')}"
    except Exception:
        pass

    return render_template(
        "item.html",
        section=section,
        item=item,
        season_rating_key=season_rating_key,
        show_seasons=show_seasons,
        selected_season=selected_season,
        season_episodes=season_episodes,
        selected_episode=selected_episode,
        local_files=local_files,
        art_panel=art_panel,
        item_page=item_page,
        item_letter=item_letter,
        pages=pages,
        art_type=art_type,
        season=season,
        episode=episode,
        art_page=art_page,
        missing_type=missing_type,
        plex_web_url=plex_web_url,
        imdb_url=imdb_url,
        tvdb_url=tvdb_url,
        tmdb_url=tmdb_url,
        mediux_url=mediux_url,
    )


def plex_art_candidates(source, art_type):
    """Images Plex offers for this item, for the chosen art type."""
    if art_type == "poster":
        return source.posters()
    if art_type == "background":
        return source.arts()
    # logo / square art: use plexapi's own method when this version has one,
    # else ask the Plex endpoint directly; an old server that doesn't know the
    # endpoint just means "nothing offered" (a URL or a file can still be used)
    methods = {"logo": ("logos", "clearLogos"), "square": ("squareArts", "squareArt")}[art_type]
    endpoint = {"logo": "clearLogos", "square": "squareArts"}[art_type]
    for name in methods:
        method = getattr(source, name, None)
        if callable(method):
            try:
                return list(method())
            except Exception:
                break
    try:
        from plexapi import media as plex_media
        return source.fetchItems(f"/library/metadata/{source.ratingKey}/{endpoint}", cls=plex_media.Poster)
    except Exception:
        return []


app.jinja_env.globals["plex_art_candidates"] = plex_art_candidates


def compute_asset_base_dir(item, season_item):
    asset_root = os.path.join(os.getcwd(), session["asset_dir"])
    return _asset_folder_for(item, asset_root, for_write=True)


def compute_asset_filename(item, art_type, season, episode, ext):
    if item.type == "show":
        if episode:
            s, e = int(season), int(episode)
            name = f"S{s:02d}E{e:02d}"
        elif season:
            s = int(season)
            name = f"Season{s:02d}"
        else:
            name = art_type
        suffix = (
            f"_{art_type}" if art_type == "background" and (season or episode) else ""
        )
        return f"{name}{suffix}{ext}"
    return f"{art_type}{ext}"


def get_download_context(form):
    """Shared form fields used by /download, /upload_url and /upload_file."""
    rating_key = int(form["rating_key"])
    try:
        season_rating_key = int(form["season_rating_key"])
    except:
        season_rating_key = None
    art_type = form["art_type"]
    if art_type not in ART_TYPES:  # it ends up in a file name: only known values
        abort(400)
    season = None if form.get("season") == "None" else form.get("season")
    episode = None if form.get("episode") == "None" else form.get("episode")
    if art_type in ART_EXTRAS:  # logos / square art only exist at movie / show level
        season = episode = season_rating_key = None
    return {
        "rating_key": rating_key,
        "season_rating_key": season_rating_key,
        "section_key": form["section_key"],
        "art_type": art_type,
        "season": season,
        "episode": episode,
        "item_page": form.get("item_page", 1),
        "art_page": form.get("art_page", 1),
        "missing_type": form.get("missing_type", ""),
    }


@app.route("/download", methods=["POST"])
def download():
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))

    ctx = get_download_context(request.form)
    img_key = request.form["img_key"]

    item = plex.fetchItem(ctx["rating_key"])
    season_item = (
        None if not ctx["season_rating_key"] else plex.fetchItem(ctx["season_rating_key"])
    )

    base_dir = compute_asset_base_dir(item, season_item)
    os.makedirs(base_dir, exist_ok=True)
    _remember_dir(os.path.join(os.getcwd(), session["asset_dir"]), base_dir)

    ext = os.path.splitext(img_key)[1] or (".png" if ctx["art_type"] == "logo" else ".jpg")
    filename = compute_asset_filename(item, ctx["art_type"], ctx["season"], ctx["episode"], ext)

    if img_key.startswith("http"):
        img_url = img_key
    else:
        img_url = f"{session['base_url']}{img_key}&X-Plex-Token={get_token()}"

    resp = requests.get(img_url)
    with open(os.path.join(base_dir, filename), "wb") as f:
        f.write(resp.content)

    # a new file just landed on disk: refresh that folder in the cached scan and
    # the dashboard counts right away, so badges and rings are correct at once
    after_asset_write(item, base_dir)

    flash(f"Saved to {os.path.join(base_dir, filename)}")
    return redirect(
        url_for(
            "browse",
            section_key=ctx["section_key"],
            page=ctx["item_page"],
            art_type=ctx["art_type"],
            season=ctx["season"],
            episode=ctx["episode"],
            art_page=ctx["art_page"],
            missing_type=ctx["missing_type"] if ctx["missing_type"] else None,
        )
    )


@app.route("/upload_url", methods=["POST"])
def upload_url():
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))

    ctx = get_download_context(request.form)
    image_url = request.form.get("image_url", "").strip()

    if not image_url:
        flash("No URL provided.")
        return redirect(
            url_for(
                "browse",
                section_key=ctx["section_key"],
                page=ctx["item_page"],
                art_type=ctx["art_type"],
                season=ctx["season"],
                episode=ctx["episode"],
                art_page=ctx["art_page"],
            missing_type=ctx["missing_type"] if ctx["missing_type"] else None,
            )
        )

    item = plex.fetchItem(ctx["rating_key"])
    season_item = (
        None if not ctx["season_rating_key"] else plex.fetchItem(ctx["season_rating_key"])
    )

    base_dir = compute_asset_base_dir(item, season_item)
    os.makedirs(base_dir, exist_ok=True)
    _remember_dir(os.path.join(os.getcwd(), session["asset_dir"]), base_dir)

    ext = os.path.splitext(image_url.split("?")[0])[1] or (".png" if ctx["art_type"] == "logo" else ".jpg")
    filename = compute_asset_filename(item, ctx["art_type"], ctx["season"], ctx["episode"], ext)

    try:
        resp = requests.get(image_url, timeout=15)
        resp.raise_for_status()
        with open(os.path.join(base_dir, filename), "wb") as f:
            f.write(resp.content)
        after_asset_write(item, base_dir)
        flash(f"Saved to {os.path.join(base_dir, filename)}")
    except Exception as e:
        flash(f"Download from URL failed: {e}")

    return redirect(
        url_for(
            "browse",
            section_key=ctx["section_key"],
            page=ctx["item_page"],
            art_type=ctx["art_type"],
            season=ctx["season"],
            episode=ctx["episode"],
            art_page=ctx["art_page"],
            missing_type=ctx["missing_type"] if ctx["missing_type"] else None,
        )
    )


@app.route("/upload_file", methods=["POST"])
def upload_file():
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))

    ctx = get_download_context(request.form)
    uploaded = request.files.get("image_file")

    if not uploaded or uploaded.filename == "":
        flash("No files selected.")
        return redirect(
            url_for(
                "browse",
                section_key=ctx["section_key"],
                page=ctx["item_page"],
                art_type=ctx["art_type"],
                season=ctx["season"],
                episode=ctx["episode"],
                art_page=ctx["art_page"],
            missing_type=ctx["missing_type"] if ctx["missing_type"] else None,
            )
        )

    item = plex.fetchItem(ctx["rating_key"])
    season_item = (
        None if not ctx["season_rating_key"] else plex.fetchItem(ctx["season_rating_key"])
    )

    base_dir = compute_asset_base_dir(item, season_item)
    os.makedirs(base_dir, exist_ok=True)
    _remember_dir(os.path.join(os.getcwd(), session["asset_dir"]), base_dir)

    ext = os.path.splitext(uploaded.filename)[1] or (".png" if ctx["art_type"] == "logo" else ".jpg")
    filename = compute_asset_filename(item, ctx["art_type"], ctx["season"], ctx["episode"], ext)

    uploaded.save(os.path.join(base_dir, filename))

    after_asset_write(item, base_dir)
    flash(f"Saved to {os.path.join(base_dir, filename)}")

    return redirect(
        url_for(
            "browse",
            section_key=ctx["section_key"],
            page=ctx["item_page"],
            art_type=ctx["art_type"],
            season=ctx["season"],
            episode=ctx["episode"],
            art_page=ctx["art_page"],
            missing_type=ctx["missing_type"] if ctx["missing_type"] else None,
        )
    )


# restore the last saved verification/dashboard state (see "persistence" above)
load_state()
start_auto_refresh()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)