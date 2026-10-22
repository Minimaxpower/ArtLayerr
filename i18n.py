"""Tiny gettext-style translation support for ArtLayerr (standard library only).

Translations live in translations/<code>.po (plain gettext files, editable with
Poedit, Weblate or any text editor). Nothing is compiled: the .po files are read
once at start-up. English is the language of the source texts, so it needs no
file. A language is available as soon as its .po file exists.

    _("Saved to %(path)s", path=p)               one text, optional placeholders
    ngettext("%(n)s image", "%(n)s images", n)   singular / plural ("n" is added)

A text that is missing from a language falls back to the English one.
"""
import ast
import os
import re

DEFAULT_LANGUAGE = "en"
_PLURAL_DEFAULT = "n != 1"


def _unescape(raw):
    out, i = [], 0
    table = {"n": "\n", "t": "\t", '"': '"', "\\": "\\", "r": "\r"}
    while i < len(raw):
        c = raw[i]
        if c == "\\" and i + 1 < len(raw):
            out.append(table.get(raw[i + 1], raw[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _quoted(line):
    start, end = line.find('"'), line.rfind('"')
    return _unescape(line[start + 1:end]) if start != -1 and end > start else ""


def parse_po(text):
    """-> (header dict, {msgid: [msgstr, ...]}). Fuzzy and empty entries are skipped."""
    entries = {}
    cur = None            # the entry being read
    field = None
    fuzzy = False

    def flush():
        nonlocal cur, fuzzy
        if cur is not None and cur.get("id") is not None:
            strs = [cur["str"][k] for k in sorted(cur["str"])]
            if not fuzzy and any(strs):
                entries[cur["id"]] = strs
        cur, fuzzy = None, False

    for line in text.splitlines():
        s = line.strip()
        if not s:
            flush()
            continue
        if s.startswith("#"):
            if s.startswith("#,") and "fuzzy" in s:
                fuzzy = True
            continue
        if s.startswith("msgctxt"):
            field = "ctx"
            continue
        if s.startswith("msgid_plural"):
            field = ("plural",)
            cur = cur if cur is not None else {"id": None, "str": {}}
            cur["plural"] = _quoted(s)
            continue
        if s.startswith("msgid"):
            if cur is not None and cur.get("str"):
                flush()
            cur = cur if cur is not None else {"id": None, "str": {}}
            cur["id"] = _quoted(s)
            field = ("id",)
            continue
        if s.startswith("msgstr"):
            cur = cur if cur is not None else {"id": None, "str": {}}
            m = re.match(r"msgstr\[(\d+)\]", s)
            idx = int(m.group(1)) if m else 0
            cur["str"][idx] = _quoted(s)
            field = ("str", idx)
            continue
        if s.startswith('"') and cur is not None and field:
            piece = _quoted(s)
            if field[0] == "id":
                cur["id"] += piece
            elif field[0] == "plural":
                cur["plural"] += piece
            elif field[0] == "str":
                cur["str"][field[1]] += piece
    flush()

    header = {}
    for raw in (entries.pop("", [""])[0]).splitlines():
        if ":" in raw:
            k, v = raw.split(":", 1)
            header[k.strip().lower()] = v.strip()
    return header, entries


# ---- plural rules: "nplurals=2; plural=(n > 1);" ------------------------------
def _split_top(expr, ch):
    depth = 0
    for i, c in enumerate(expr):
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == ch and depth == 0:
            return i
    return -1


def _c_to_py(expr):
    expr = expr.strip()
    q = _split_top(expr, "?")
    if q != -1:
        cond, rest = expr[:q], expr[q + 1:]
        # the matching ':' is the first top-level one that is not part of a nested ?:
        depth_q, colon = 0, -1
        paren = 0
        for i, c in enumerate(rest):
            if c == "(":
                paren += 1
            elif c == ")":
                paren -= 1
            elif paren == 0 and c == "?":
                depth_q += 1
            elif paren == 0 and c == ":":
                if depth_q == 0:
                    colon = i
                    break
                depth_q -= 1
        if colon == -1:
            raise ValueError("bad plural expression")
        a, b = rest[:colon], rest[colon + 1:]
        return f"(({_c_to_py(a)}) if ({_c_to_py(cond)}) else ({_c_to_py(b)}))"
    return expr.replace("&&", " and ").replace("||", " or ")


_ALLOWED = (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.BinOp, ast.Mod, ast.Compare,
            ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.IfExp, ast.UnaryOp,
            ast.Not, ast.Constant, ast.Name, ast.Load)


def plural_function(header_value):
    """Build n -> index from a Plural-Forms header (safe: only a tiny subset is evaluated)."""
    m = re.search(r"nplurals\s*=\s*(\d+)\s*;\s*plural\s*=\s*([^;]+)", header_value or "")
    nplurals = int(m.group(1)) if m else 2
    expr = m.group(2) if m else _PLURAL_DEFAULT
    try:
        py = _c_to_py(expr)
        tree = ast.parse(py, mode="eval")
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED):
                raise ValueError("unsupported plural expression")
            if isinstance(node, ast.Name) and node.id != "n":
                raise ValueError("unsupported plural expression")
        code = compile(tree, "<plural>", "eval")
    except (ValueError, SyntaxError):
        code = compile(_PLURAL_DEFAULT, "<plural>", "eval")
        nplurals = 2

    def fn(n):
        idx = eval(code, {"__builtins__": {}}, {"n": n})  # noqa: S307 - validated above
        idx = int(idx)
        return idx if 0 <= idx < nplurals else 0
    return fn


class Translator:
    def __init__(self, directory):
        self.directory = directory
        self.catalogs = {}   # code -> {"strings": {...}, "plural": fn, "name": "Français"}
        self.reload()

    def reload(self):
        self.catalogs = {}
        if not os.path.isdir(self.directory):
            return
        for fname in sorted(os.listdir(self.directory)):
            if not fname.endswith(".po"):
                continue
            code = fname[:-3]
            try:
                with open(os.path.join(self.directory, fname), encoding="utf-8") as f:
                    header, strings = parse_po(f.read())
            except OSError:
                continue
            self.catalogs[code] = {
                "strings": strings,
                "plural": plural_function(header.get("plural-forms")),
                "name": header.get("x-native-name") or code,
            }

    @property
    def codes(self):
        return [DEFAULT_LANGUAGE] + [c for c in self.catalogs if c != DEFAULT_LANGUAGE]

    def languages(self):
        """[(code, native name)] for the language picker, English first."""
        return [(DEFAULT_LANGUAGE, "English")] + [
            (c, self.catalogs[c]["name"]) for c in self.catalogs if c != DEFAULT_LANGUAGE
        ]

    @staticmethod
    def _fill(text, kwargs):
        if not kwargs:
            return text
        try:
            return text % kwargs
        except (KeyError, ValueError, TypeError):
            return text

    def gettext(self, lang, msgid, **kwargs):
        cat = self.catalogs.get(lang)
        found = cat["strings"].get(msgid) if cat else None
        text = found[0] if found and found[0] else msgid
        if kwargs:
            filled = self._fill(text, kwargs)
            if filled == text and text != msgid and "%(" in text:
                # a broken translation (wrong placeholder): use the English text
                return self._fill(msgid, kwargs)
            return filled
        return text

    def ngettext(self, lang, singular, plural, n, **kwargs):
        kwargs.setdefault("n", n)
        cat = self.catalogs.get(lang)
        found = cat["strings"].get(singular) if cat else None
        if found and len(found) > 1:
            idx = cat["plural"](n)
            text = found[idx] if idx < len(found) and found[idx] else (singular if n == 1 else plural)
        else:
            text = singular if n == 1 else plural
        return self._fill(text, kwargs)
