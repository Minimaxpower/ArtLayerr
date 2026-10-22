#!/usr/bin/env python3
"""Translation helper for ArtLayerr.

  python tools/translations.py pot     write translations/messages.pot (every text to translate)
  python tools/translations.py check   compare every translations/*.po with the code; exit code 1
                               when a text is missing or a placeholder / HTML tag differs
  python tools/translations.py sync    add the texts that are missing to every translations/*.po
                               (empty translation, nothing is ever removed)

Texts are found where the code marks them: _("...") and ngettext("...", "...", n)
in templates/*.html, tr("..."), ntr("...", "...", n) and N_("...") in app.py.
To add a language: copy translations/messages.pot to translations/<code>.po,
fill in the header (Language, Plural-Forms, X-Native-Name) and translate.
"""
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import i18n  # noqa: E402

TEMPLATES = os.path.join(ROOT, "templates")
TRANSLATIONS = os.path.join(ROOT, "translations")
PY_FILES = [os.path.join(ROOT, "app.py")]

_STR = r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
_T_SINGLE = re.compile(r"(?<![A-Za-z0-9_])_\(\s*" + _STR)
_T_PLURAL = re.compile(r"(?<![A-Za-z0-9_])ngettext\(\s*" + _STR + r"\s*,\s*" + _STR)


def _jinja_str(token):
    body = token[1:-1]
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), body)


def collect():
    """{msgid: {"plural": str|None, "refs": [places]}}"""
    found = {}

    def add(msgid, plural, ref):
        entry = found.setdefault(msgid, {"plural": plural, "refs": []})
        if plural and not entry["plural"]:
            entry["plural"] = plural
        if ref not in entry["refs"]:
            entry["refs"].append(ref)

    if os.path.isdir(TEMPLATES):
        for fname in sorted(os.listdir(TEMPLATES)):
            if not fname.endswith(".html"):
                continue
            with open(os.path.join(TEMPLATES, fname), encoding="utf-8") as f:
                text = f.read()
            for m in _T_PLURAL.finditer(text):
                add(_jinja_str(m.group(1)), _jinja_str(m.group(2)), f"templates/{fname}")
            for m in _T_SINGLE.finditer(text):
                add(_jinja_str(m.group(1)), None, f"templates/{fname}")
    for path in PY_FILES:
        rel = os.path.relpath(path, ROOT)
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            name = node.func.id
            strs = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if name in ("tr", "N_") and node.args and isinstance(node.args[0], ast.Constant):
                add(node.args[0].value, None, f"{rel}:{node.lineno}")
            elif name == "ntr" and len(strs) >= 2 and isinstance(node.args[0], ast.Constant):
                add(node.args[0].value, node.args[1].value, f"{rel}:{node.lineno}")
    return found


def _po_quote(text):
    lines = text.split("\n")
    esc = lambda t: t.replace("\\", "\\\\").replace('"', '\\"')
    if len(lines) == 1:
        return f'"{esc(lines[0])}"'
    return '""\n' + "\n".join(f'"{esc(l)}\\n"' for l in lines[:-1]) + (f'\n"{esc(lines[-1])}"' if lines[-1] else "")


def _entry(msgid, plural, refs, translated=None):
    out = ["#: " + " ".join(refs[:3])]
    if "%(" in msgid:
        out.append("#, python-format")
    out.append(f"msgid {_po_quote(msgid)}")
    if plural:
        out.append(f"msgid_plural {_po_quote(plural)}")
        tr = translated or ["", ""]
        for i, t in enumerate(tr):
            out.append(f"msgstr[{i}] {_po_quote(t)}")
    else:
        out.append(f"msgstr {_po_quote(translated[0] if translated else '')}")
    return "\n".join(out)


def write_pot(found):
    head = '''# ArtLayerr translations: template. Copy to <code>.po to add a language.
msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"
"Language: \\n"
"Plural-Forms: nplurals=2; plural=(n != 1);\\n"
"X-Native-Name: \\n"
'''
    body = "\n\n".join(_entry(k, v["plural"], v["refs"]) for k, v in sorted(found.items()))
    path = os.path.join(TRANSLATIONS, "messages.pot")
    os.makedirs(TRANSLATIONS, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n" + body + "\n")
    return path


def _po_files():
    if not os.path.isdir(TRANSLATIONS):
        return []
    return [os.path.join(TRANSLATIONS, n) for n in sorted(os.listdir(TRANSLATIONS)) if n.endswith(".po")]


_PH = re.compile(r"%\([A-Za-z_][A-Za-z0-9_]*\)s")
_TAG = re.compile(r"</?[A-Za-z][^>]*>")


def check(found):
    problems = 0
    for path in _po_files():
        with open(path, encoding="utf-8") as f:
            header, strings = i18n.parse_po(f.read())
        name = os.path.basename(path)
        missing = [k for k in found if k not in strings]
        stale = [k for k in strings if k not in found]
        bad = []
        for msgid, info in found.items():
            tr = strings.get(msgid)
            if not tr:
                continue
            forms = [msgid] + ([info["plural"]] if info["plural"] else [])
            for i, t in enumerate(tr):
                src = forms[min(i, len(forms) - 1)] if info["plural"] else msgid
                if sorted(_PH.findall(t)) != sorted(_PH.findall(src)) and not (info["plural"] and i == 0 and "%(n)s" not in t):
                    bad.append((msgid, "placeholders differ"))
                if sorted(re.findall(r"</?(\w+)", t)) != sorted(re.findall(r"</?(\w+)", src)):
                    bad.append((msgid, "HTML tags differ"))
            if info["plural"] and len(tr) < 2:
                bad.append((msgid, "plural form missing"))
        print(f"{name}: {len(found) - len(missing)}/{len(found)} translated, {len(stale)} no longer used, {len(bad)} to fix")
        for k in missing[:50]:
            print(f"   missing : {k[:100]!r}")
        for k, why in bad[:50]:
            print(f"   {why}: {k[:100]!r}")
        problems += len(missing) + len(bad)
    if not _po_files():
        print("no translations/*.po file found")
    return problems


def sync(found):
    for path in _po_files():
        with open(path, encoding="utf-8") as f:
            raw = f.read()
        _, strings = i18n.parse_po(raw)
        add = [k for k in sorted(found) if k not in strings]
        if not add:
            print(f"{os.path.basename(path)}: nothing to add")
            continue
        extra = "\n\n".join(_entry(k, found[k]["plural"], found[k]["refs"]) for k in add)
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n\n" + extra + "\n")
        print(f"{os.path.basename(path)}: {len(add)} text(s) added, to translate")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    found = collect()
    if cmd == "pot":
        print(f"{len(found)} texts -> {write_pot(found)}")
    elif cmd == "check":
        sys.exit(1 if check(found) else 0)
    elif cmd == "sync":
        sync(found)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
