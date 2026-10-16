import os
import math
import string
import time
import datetime
import json
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
import requests


def sort_key(title):
    """Normalize to NFC before lowercasing for sorting. Plex can return
    accented titles in either precomposed or decomposed Unicode form
    depending on the source — without this, e.g. 'Émissions' (decomposed)
    sorts before 'Séries' because its first codepoint is a bare 'e', while
    the same title in precomposed form would sort correctly after it."""
    return unicodedata.normalize("NFC", title or "").lower()
from flask import Flask, flash, redirect, render_template, request, session, url_for
from plexapi.server import PlexServer

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(24)

# module‑level alphabet list so we only build it once
ALPHABET = list(string.ascii_uppercase)
ALPHABET.insert(0, "0-9")

# cache of non-empty asset folders, split between poster files and background
# files, per media type. Scanning this means checking every item's folder on
# disk, which is the expensive part of this app — unlike before, this is now
# PURELY MANUAL: it is never recomputed automatically on a page view or after
# a timeout. It is only (re)computed when the "🔄 Update" button
# is used, or right after a download (a single, cheap, known-fresh update).
# computed_at stays None until the first manual check ever happens.
_ASSET_CACHE = {
    "movie_posters": set(),
    "movie_backgrounds": set(),
    "series_posters": set(),
    "series_backgrounds": set(),
    "series_season_episode": {},
    "computed_at": None,
}

_SEASON_FILE_RE = re.compile(r"^season(\d+)", re.IGNORECASE)
_EPISODE_FILE_RE = re.compile(r"^s\d+e\d+", re.IGNORECASE)


def _scan_asset_folders(root):
    """Scan `root` (assets/movies or assets/series) and return two sets of
    folder names: those containing a poster.* file, and those containing a
    background.* file, directly inside them (files are flat, no subfolders)."""
    posters = set()
    backgrounds = set()
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                try:
                    with os.scandir(entry.path) as files:
                        for f in files:
                            if not f.is_file():
                                continue
                            name = f.name.lower()
                            if name.startswith("poster"):
                                posters.add(entry.name)
                            elif name.startswith("background"):
                                backgrounds.add(entry.name)
                except (FileNotFoundError, PermissionError):
                    pass
    except (FileNotFoundError, PermissionError):
        pass
    return posters, backgrounds


def _scan_series_season_episode_assets(root):
    """Scan assets/series and, per show folder, collect which season numbers
    have a custom SeasonXX.* poster and how many SxxExx.* episode title cards
    exist. Purely file-based — this never looks at what Plex itself currently
    has selected, only what's actually sitting in the assets folder."""
    result = {}
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir():
                    continue
                seasons = set()
                episode_count = 0
                try:
                    with os.scandir(entry.path) as files:
                        for f in files:
                            if not f.is_file():
                                continue
                            m = _SEASON_FILE_RE.match(f.name)
                            if m:
                                seasons.add(int(m.group(1)))
                                continue
                            if _EPISODE_FILE_RE.match(f.name):
                                episode_count += 1
                except (FileNotFoundError, PermissionError):
                    pass
                if seasons or episode_count:
                    result[entry.name] = {"seasons": seasons, "episode_count": episode_count}
    except (FileNotFoundError, PermissionError):
        pass
    return result


def get_asset_folders(asset_root, force=False):
    """Return the cached scan. Never scans on its own — pass force=True to
    actually (re)do the disk scan (used by the manual refresh button and
    right after a download)."""
    if force:
        _ASSET_CACHE["movie_posters"], _ASSET_CACHE["movie_backgrounds"] = _scan_asset_folders(
            os.path.join(asset_root, "movies")
        )
        _ASSET_CACHE["series_posters"], _ASSET_CACHE["series_backgrounds"] = _scan_asset_folders(
            os.path.join(asset_root, "series")
        )
        _ASSET_CACHE["series_season_episode"] = _scan_series_season_episode_assets(
            os.path.join(asset_root, "series")
        )
        _ASSET_CACHE["computed_at"] = time.time()
        save_state()
    return _ASSET_CACHE


def annotate_asset_flags(items):
    """Set item.has_poster / item.has_background (and, for shows, custom
    season/episode info) for each item, from the cached scan. If no manual
    check has ever been run yet, everything is set to None (meaning "unknown
    / not checked"), never guessed as False."""
    asset_root = os.path.join(os.getcwd(), session.get("asset_dir", "assets"))
    cache = get_asset_folders(asset_root)  # force=False: just reads, never scans
    checked = cache["computed_at"] is not None
    for item in items:
        if not checked:
            item.has_poster = None
            item.has_background = None
            item.custom_seasons = None
            item.custom_episode_count = None
            item.custom_seasons_display = None
            item.seasons_complete = None
            item.episodes_complete = None
            continue
        try:
            if item.type == "movie":
                media_file = item.media[0].parts[0].file
                asset_name = os.path.basename(os.path.dirname(media_file))
                item.has_poster = asset_name in cache["movie_posters"]
                item.has_background = asset_name in cache["movie_backgrounds"]
                item.custom_seasons = None
                item.custom_episode_count = None
                item.custom_seasons_display = None
                item.seasons_complete = None
                item.episodes_complete = None
            else:
                asset_name = os.path.basename(item.locations[0])
                item.has_poster = asset_name in cache["series_posters"]
                item.has_background = asset_name in cache["series_backgrounds"]
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

# which attribute each filter type checks, and whether it's show-only
MISSING_TYPES = {
    "poster": {"label": "poster", "show_only": False},
    "background": {"label": "background", "show_only": False},
    "season": {"label": "poster de saison", "show_only": True},
    "episode": {"label": "image d'épisode", "show_only": True},
}


def _is_missing(item, missing_type):
    if missing_type == "poster":
        return not item.has_poster
    if missing_type == "background":
        return not item.has_background
    if missing_type == "season":
        return not item.custom_seasons
    if missing_type == "episode":
        return not item.custom_episode_count
    return False


def get_missing_rating_keys(section, missing_type="poster", force=False):
    if missing_type not in MISSING_TYPES:
        missing_type = "poster"
    if _ASSET_CACHE["computed_at"] is None:
        return None  # nothing has ever been checked — caller must handle this
    key = (str(section.key), missing_type)
    entry = _MISSING_CACHE.get(key)
    if force or not entry or entry["scanned_at"] < _ASSET_CACHE["computed_at"]:
        items = list(section.all())
        annotate_asset_flags(items)
        entry = {
            "keys": [i.ratingKey for i in items if _is_missing(i, missing_type)],
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
# demand (first visit, or an explicit "Actualiser" click), never on a timer.
_DASHBOARD_CACHE = {"data": None, "computed_at": None}


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
    base = os.environ.get("STATE_DIR") or os.environ.get("PLEX_ASSET_DIR", "assets")
    return os.path.join(os.getcwd(), base, _STATE_FILE_NAME)


def save_state():
    try:
        series_se = {
            name: {"seasons": sorted(info["seasons"]), "episode_count": info["episode_count"]}
            for name, info in _ASSET_CACHE["series_season_episode"].items()
        }
        payload = {
            "version": _STATE_VERSION,
            "asset_cache": {
                "movie_posters": sorted(_ASSET_CACHE["movie_posters"]),
                "movie_backgrounds": sorted(_ASSET_CACHE["movie_backgrounds"]),
                "series_posters": sorted(_ASSET_CACHE["series_posters"]),
                "series_backgrounds": sorted(_ASSET_CACHE["series_backgrounds"]),
                "series_season_episode": series_se,
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
            _ASSET_CACHE["computed_at"] = ac["computed_at"]
        dash = payload.get("dashboard") or {}
        if dash.get("data") and dash.get("computed_at"):
            _DASHBOARD_CACHE["data"] = dash["data"]
            _DASHBOARD_CACHE["computed_at"] = dash["computed_at"]
    except Exception:
        pass  # missing or unreadable file: just start empty, as before


def compute_dashboard_stats(plex):
    sections = sorted(plex.library.sections(), key=lambda s: sort_key(s.title))
    library_stats = []
    totals = {
        "total_items": 0,
        "poster_done": 0, "poster_total": 0,
        "bg_done": 0, "bg_total": 0,
        # season/episode coverage only exists for shows, tracked separately so
        # the percentage isn't diluted by movie libraries that have neither
        "season_done": 0, "season_total": 0,
        "episode_done": 0, "episode_total": 0,
    }

    for section in sections:
        if section.type not in ("movie", "show"):
            continue
        try:
            items = list(section.all())
        except requests.exceptions.RequestException:
            continue
        annotate_asset_flags(items)
        total = len(items)
        poster_done = sum(1 for i in items if i.has_poster)
        bg_done = sum(1 for i in items if i.has_background)

        lib_entry = {
            "key": section.key,
            "title": section.title,
            "type": section.type,
            "total": total,
            "poster_done": poster_done,
            "bg_done": bg_done,
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

            totals["season_done"] += season_done
            totals["season_total"] += season_total
            totals["episode_done"] += episode_done
            totals["episode_total"] += episode_total
        else:
            lib_entry["season_done"] = None
            lib_entry["season_total"] = None
            lib_entry["episode_done"] = None
            lib_entry["episode_total"] = None

        library_stats.append(lib_entry)
        totals["total_items"] += total
        totals["poster_done"] += poster_done
        totals["poster_total"] += total
        totals["bg_done"] += bg_done
        totals["bg_total"] += total

    library_stats.sort(key=lambda lib: (lib["type"], sort_key(lib["title"])))
    return {"library_stats": library_stats, "totals": totals}


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
            "PyYAML n'est pas installé dans le conteneur : ajoute « PyYAML » à "
            "requirements.txt puis rebuild. En attendant, seule la comparaison "
            "des séries (sans le détail saisons/épisodes) est disponible."
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
        where = f" (ligne {mark.line + 1}, colonne {mark.column + 1})" if mark else ""
        problem = getattr(e, "problem", None) or str(e)
        return None, f"Erreur de syntaxe YAML{where} : {problem}", []

    warnings = [
        f"Clé « {key} » en double à la ligne {line} : seule la dernière occurrence est prise en compte."
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


def get_plex():
    base_url = session.get("base_url")
    token = session.get("token")
    if not base_url or not token:
        return None
    # default plexapi timeout is 30s; a big library or a busy Plex server
    # (e.g. mid-scan, or Kometa running) can take longer than that to answer
    # a full "section.all()" request, so give it more room before giving up
    return PlexServer(base_url, token, timeout=120)


@app.route("/", methods=["GET", "POST"])
def connect():
    if request.method == "POST":
        session["base_url"] = request.form["base_url"]
        session["token"] = request.form["token"]
        session["asset_dir"] = (
            "assets" if not request.form["asset_dir"] else request.form["asset_dir"]
        )
        return redirect(url_for("libraries"))

    # optional defaults from environment variables (set in docker-compose.yml)
    env_base_url = os.environ.get("PLEX_BASE_URL", "")
    env_token = os.environ.get("PLEX_TOKEN", "")
    env_asset_dir = os.environ.get("PLEX_ASSET_DIR", "assets")

    # if both URL and token are provided via env, skip the form entirely
    if env_base_url and env_token and not session.get("base_url"):
        session["base_url"] = env_base_url
        session["token"] = env_token
        session["asset_dir"] = env_asset_dir
        return redirect(url_for("libraries"))

    return render_template(
        "connect.html",
        default_base_url=env_base_url,
        default_token=env_token,
        default_asset_dir=env_asset_dir,
    )


@app.route("/libraries")
def libraries():
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))

    if _DASHBOARD_CACHE["data"] is None or request.args.get("refresh") == "1":
        refresh_asset_check(plex)
        _DASHBOARD_CACHE["data"] = compute_dashboard_stats(plex)
        _DASHBOARD_CACHE["computed_at"] = time.time()
        save_state()

    computed_at_display = None
    if _DASHBOARD_CACHE["computed_at"]:
        computed_at_display = datetime.datetime.fromtimestamp(
            _DASHBOARD_CACHE["computed_at"]
        ).strftime("%d/%m/%Y at %H:%M")

    return render_template(
        "libraries.html",
        library_stats=_DASHBOARD_CACHE["data"]["library_stats"],
        totals=_DASHBOARD_CACHE["data"]["totals"],
        computed_at=_DASHBOARD_CACHE["computed_at"],
        computed_at_display=computed_at_display,
    )


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
                flash(f"Impossible de lire « {upload.filename} » : {e}")
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

    # full list of items
    try:
        all_items_full = list(section.all())
    except requests.exceptions.RequestException:
        flash("Plex a mis trop de temps à répondre (serveur occupé ou bibliothèque volumineuse). Réessaie dans un instant.")
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
    available_missing_types = ["poster", "background"]
    if section.type == "show":
        available_missing_types += ["season", "episode"]

    # requested filter, validated against what's actually offered here
    missing_type = request.args.get("missing_type", "")
    if missing_type not in available_missing_types:
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
            if _is_missing(item, missing_type):
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
        items_filtered = [item for item in items_filtered if _is_missing(item, missing_type)]

    # pagination params (based on filtered set)
    total    = len(items_filtered)
    per_page = 24  # 4 full rows of 6 on the grid at desktop width, no orphan row
    pages    = math.ceil(total / per_page) if total else 1

    # clamp page number
    page = int(request.args.get("page", 1))
    page = max(1, min(page, pages))

    # slice out this page from filtered items
    start      = (page - 1) * per_page
    page_items = items_filtered[start : start + per_page]

    missing_counts = {
        t: (len([i for i in all_items_full if _is_missing(i, t)]) if checked else 0)
        for t in available_missing_types
    }

    return render_template(
        "items.html",
        section=section,
        checked=checked,
        items=page_items,
        page=page,
        pages=pages,
        per_page=per_page,
        alphabet=ALPHABET,
        letter=letter,
        search_query=search_query,
        missing_type=missing_type,
        missing_counts=missing_counts,
        available_missing_types=available_missing_types,
        last_check_ts=_ASSET_CACHE["computed_at"] if checked else None,
        last_check_display=(
            datetime.datetime.fromtimestamp(_ASSET_CACHE["computed_at"]).strftime("%d/%m/%Y at %H:%M")
            if checked else None
        ),
    )


@app.route("/refresh_assets/<section_key>")
def refresh_assets(section_key):
    plex = get_plex()
    if not plex:
        return redirect(url_for("connect"))

    refresh_asset_check(plex, section_key)
    flash("Vérification des posters/backgrounds effectuée.")

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
        asset_name = os.path.basename(item.locations[0])
        item_dir = os.path.join(
            os.getcwd(), session.get("asset_dir", "assets"), "series", asset_name
        )
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
    available_missing_types = ["poster", "background"]
    if section.type == "show":
        available_missing_types += ["season", "episode"]
    missing_type = request.args.get("missing_type", "")
    if missing_type not in available_missing_types:
        missing_type = ""

    if missing_type:
        try:
            missing_keys = get_missing_rating_keys(section, missing_type=missing_type)
        except requests.exceptions.RequestException:
            flash("Plex a mis trop de temps à répondre (serveur occupé ou bibliothèque volumineuse). Réessaie dans un instant.")
            return redirect(url_for("libraries"))
        if missing_keys is None:
            flash("Lance d'abord une vérification avant d'utiliser ce filtre.")
            return redirect(url_for("list_items", section_key=section.key))
        pages = len(missing_keys)
        if not pages:
            label = MISSING_TYPES[missing_type]["label"]
            flash(f"Tous les items ont déjà un {label} \u2014 filtre retiré.")
            return redirect(
                url_for("list_items", section_key=section.key, missing_type=missing_type)
            )
        item_page = int(request.args.get("page", 1))
        item_letter = request.args.get("letter", "All")
        item_page = max(1, min(item_page, pages))
        item = plex.fetchItem(missing_keys[item_page - 1])
    else:
        try:
            items = list(section.all())
        except requests.exceptions.RequestException:
            flash("Plex a mis trop de temps à répondre (serveur occupé ou bibliothèque volumineuse). Réessaie dans un instant.")
            return redirect(url_for("libraries"))
        pages = len(items)
        item_page = int(request.args.get("page", 1))
        item_letter = request.args.get("letter", "All")
        item_page = max(1, min(item_page, pages))
        item = items[item_page - 1]

    # same badges shown on the items.html grid, for a quick reminder here too
    annotate_asset_flags([item])

    art_type = request.args.get("art_type", "poster")
    season = request.args.get("season") or None
    episode = request.args.get("episode") or None
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
            if episode == "first":  # set by the "Épisodes" tab, resolved only on click
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

    # direct link to this item's page in the Plex web app
    try:
        plex_web_url = (
            f"{session['base_url']}/web/index.html#!/server/"
            f"{plex.machineIdentifier}/details?key=%2Flibrary%2Fmetadata%2F{item.ratingKey}"
        )
    except Exception:
        plex_web_url = None

    # IMDb link, if Plex has an IMDb guid for this item
    imdb_url = None
    try:
        for guid in item.guids:
            if guid.id.startswith("imdb://"):
                imdb_url = f"https://www.imdb.com/title/{guid.id.replace('imdb://', '')}/"
                break
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
    )


def compute_asset_base_dir(item, season_item):
    try:
        asset_name = None
        if item.type == "movie":
            media_file = item.media[0].parts[0].file
        elif item.type == "show" or season_item:
            media_file = item.locations[0]
            asset_name = os.path.basename(media_file)
        elif item.type == "episode":
            media_file = item.media[0].parts[0].file
        else:
            media_file = None
        if not asset_name:
            asset_name = os.path.basename(os.path.dirname(media_file))
    except Exception:
        asset_name = item.title

    return os.path.join(
        os.getcwd(),
        session["asset_dir"],
        "movies" if item.type == "movie" else "series",
        asset_name,
    )


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
    return {
        "rating_key": rating_key,
        "season_rating_key": season_rating_key,
        "section_key": form["section_key"],
        "art_type": form["art_type"],
        "season": None if form.get("season") == "None" else form.get("season"),
        "episode": None if form.get("episode") == "None" else form.get("episode"),
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

    ext = os.path.splitext(img_key)[1] or ".jpg"
    filename = compute_asset_filename(item, ctx["art_type"], ctx["season"], ctx["episode"], ext)

    if img_key.startswith("http"):
        img_url = img_key
    else:
        img_url = f"{session['base_url']}{img_key}&X-Plex-Token={session['token']}"

    resp = requests.get(img_url)
    with open(os.path.join(base_dir, filename), "wb") as f:
        f.write(resp.content)

    # a new file just landed on disk — refresh the cached "has poster" sets right
    # away instead of waiting for the TTL, so the badge is correct immediately
    get_asset_folders(
        os.path.join(os.getcwd(), session.get("asset_dir", "assets")), force=True
    )
    if _ASSET_CACHE["computed_at"] is not None:
        refresh_asset_check(plex, ctx["section_key"])

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
        flash("Aucune URL fournie.")
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

    ext = os.path.splitext(image_url.split("?")[0])[1] or ".jpg"
    filename = compute_asset_filename(item, ctx["art_type"], ctx["season"], ctx["episode"], ext)

    try:
        resp = requests.get(image_url, timeout=15)
        resp.raise_for_status()
        with open(os.path.join(base_dir, filename), "wb") as f:
            f.write(resp.content)
        get_asset_folders(
            os.path.join(os.getcwd(), session.get("asset_dir", "assets")), force=True
        )
        if _ASSET_CACHE["computed_at"] is not None:
            refresh_asset_check(plex, ctx["section_key"])
        flash(f"Saved to {os.path.join(base_dir, filename)}")
    except Exception as e:
        flash(f"Échec du téléchargement depuis l'URL : {e}")

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

    ext = os.path.splitext(uploaded.filename)[1] or ".jpg"
    filename = compute_asset_filename(item, ctx["art_type"], ctx["season"], ctx["episode"], ext)

    uploaded.save(os.path.join(base_dir, filename))

    get_asset_folders(
        os.path.join(os.getcwd(), session.get("asset_dir", "assets")), force=True
    )
    if _ASSET_CACHE["computed_at"] is not None:
        refresh_asset_check(plex, ctx["section_key"])
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)