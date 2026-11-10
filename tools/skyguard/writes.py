r"""Shell writes (Plan 5 Task 8, E21): what a command WRITES or COPIES ONTO, and what that file is.

    scan(command, parsed, roots, cwd, env) -> Scan(deny, advice)

The Write/Edit guard refused a plugin or archive (W-PLUGIN), but a shell could write one freely: `echo x > X.esp`,
`Set-Content X.esp`, `sed -i ... X.esp` all ran with at most a text-matched note (E21). And that note
(ADV_PLUGIN, "command references plugin/archive files") fired on every READ of a plugin -- 3.3% of all calls, pure
noise (R11). Here a write is a PROGRAM that runs (shellparse) and its target is an OPERAND, classified by the
same file classes the Write guard uses (FILE CLASSES below) and, where location matters, resolved against the
payload cwd (targets).

WRITE PRIMITIVES (the target is overwritten or appended to in place):
  - an output redirect (`>`, `>>`, `>|`, `&>`, `2>`, PowerShell `*>`, cmd `>`), except a duplication (`2>&1`) and
    the null devices (/dev/null, NUL, $null);
  - `tee` (bash) and Tee-Object (-FilePath / -LiteralPath / position 0);
  - Set-Content, Add-Content, Clear-Content, New-Item (-Path / -LiteralPath / position 0), Out-File (-FilePath /
    -LiteralPath / position 0);
  - [IO.File]::Write* / Append* / Create / OpenWrite (shellparse's io.file.write / io.file.append);
  - `sed -i` (also `-i.bak`, `--in-place`, clusters such as `-Ei`): its file operands;
  - `dd of=FILE`.
  - a copy FROM a null device (`cp /dev/null X.esp`, `Copy-Item NUL X.esp`): it truncates the destination.
COPY PRIMITIVES (the destination is replaced by another file): cp, mv, rsync, Copy-Item, Move-Item, cmd
copy / move / xcopy, robocopy (its file arguments), [IO.File]::Copy / Move / Replace. A destination is classified
as written AND where it resolves (`cp a.ini x.ini` run inside Data/SKSE/Plugins writes an SKSE config).

RULES:
  - S-WRITE-PLUGIN (deny): a write primitive whose target is a plugin or archive, ANY location (like W-PLUGIN).
  - S-COPY-PLUGIN (advise; controller ruling 2026-10-02: copying the user's own built ESP into Data/ is routine,
    so this is a note, not an ask): a copy whose destination is a plugin or archive where the game loads it --
    directly in the install's Data/ folder, or in MO2 mods/ or overwrite/. A destination that is a folder takes
    the source's name (`cp My.esp Data/`). An UNRESOLVED destination counts when its own words put the plugin
    directly in a Data folder (`"$GAME/Data/My.esp"`).
  - S-WRITE-LOADORDER / S-WRITE-INI / S-WRITE-SKSE-INI (advise): a write primitive into plugins.txt/loadorder.txt,
    a Skyrim*.ini, or Data/SKSE/Plugins/*.ini -- or a copy onto one (`cp x plugins.txt` writes the load order).
  - S-WRITE-INSTALL (advise): any other write primitive into the install's Data/ folder, the config folder, or MO2
    (not the rest of the install: priced, 18 of 23 legacy redirect notes were the user's own scripts and notes).
  - S-WRITE-ARCHIVE (advise; Plan 8 T6): the same area, written by an archive writer (Compress-Archive, 7z, zip,
    tar -c), whose file is replaced or added to -- not emptied by a redirect, as the S-WRITE-INSTALL note says.
  - S-WRITE-ROOT (advise; batch-3 ruling 2026-10-13): a write primitive, a copy or a move onto a FILE DIRECTLY IN the
    install root that the game or a loader reads (ROOT_GAME_EXT: exe, dll, asi, ini, toml, cfg, json, fx: the game's
    exe and dlls, openvr_api.dll, a loader .ini, ENB/ReShade files), or a copy into the root folder itself, which
    takes the source's name. Not Data/ or any subfolder; not scratch output or the toolkit's own files there.
    A file class (an INI, the load order, a save, a plugin) keeps its own rule.
READING a plugin or plugins.txt is silent (R11).

NOT SAFE BY DEFAULT. If the parser could not read the command (ok=False, R4) the whole text is checked per
statement segment instead: a write word followed by a plugin target denies, and the class notes apply. A redirect's
target is the ONE word right after it (`a>b ... 'x.esp'` in interpreter code is a comparison; `=>`/`->` are arrows);
tee's targets are all its non-option words; the other write words take every word after them. If an
exec's program is opaque, its own words get the same segment check, and so -- when it reads a pipe -- do the words
of the nearest upstream program that is not a shell (`printf 'echo x > X.esp' | sh`). Not every exec's words: a
hook-test harness passing a write as DATA to a function beside an opaque `bash "$H"` is not a write (priced: 2).

Literal Python open/Path/truncate writes are read as Python syntax, including escaped and raw strings;
the Node literal-call and PowerShell method-call readers keep their own language rules.
KNOWN LIMITS (recorded, not handled): interpreter code that computes its filename (a Python name held in a variable, joined
or formatted, or a Path built with an operator such as `Path(a) / "x"`, is not a literal), and opaque
scripts that write a plugin get no message (R11: the Write-tool deny remains, and Plan 6's read-only lock is the
backstop); a copy whose unresolved destination does not name a Data folder gets no note (`"$D/My.esp"`);
`touch` is not a write primitive; a copy or move onto an ordinary (non-plugin, non-config) file in
Data/ gets no note (S-MOVE-INSTALL was removed in Task 9 fix round 1: 107 of 107 hits in history were the user's
deliberate deploys); on a parse failure the S-WRITE-INSTALL note is not attempted;
a target held in a variable that is NOT a same-command literal (unassigned here, assigned in a branch, written
twice; assign.py's rules) stays unresolved -- one that is (`P=X.esp; echo x > "$P"`) is judged as its literal, via
the delete rules' Exec.subst (fix-unclear 4b); `install` (`install -m644 a X.esp`) and `ln -sf a X.esp` are not
copy primitives.
"""
from __future__ import annotations

import ast
import os
import posixpath
import re
import unicodedata
from pathlib import Path
from typing import List, Mapping, NamedTuple, Optional, Tuple

from . import paths, shellparse, targets
from .deletes import _cp_target_option, _plain_operands, interpreter_code, python_nodes, robocopy_positionals, substituted
from .grep import _compile
from .shellparse import ps_param

# ------------------------------------------------------------------------------------------- file classes
# Shared with write_rules (the Edit/Write guard): targets.CLASS_RULE / targets.file_class.
PLUGIN_FILE, SKSE_INI = targets.PLUGIN_FILE, targets.SKSE_INI
CLASS_RULE = targets.CLASS_RULE
file_class = targets.file_class


# ------------------------------------------------------------------------------------------- primitives
NULL_TARGETS = frozenset(("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "nul", "nul:", "$null", "con"))
COPY_PROGRAMS = frozenset(("cp", "mv", "rsync", "copy-item", "move-item"))
CMD_COPY_PROGRAMS = frozenset(("copy", "move", "xcopy"))
_CMD_SWITCH = re.compile(r"/{1,2}[A-Za-z?\-]+(:\S*)?")      # `//E`: how Git Bash spells a switch MSYS must not rewrite
_SED_INPLACE = re.compile(r"-[nrEsuzb]*i\S*|--in-place(=\S*)?")    # P8-SED: `-bi` is in-place too
_SED_VALUED = frozenset(("-e", "-f", "-l", "--expression", "--file", "--line-length"))
_CP_VALUED = frozenset(("-S", "--suffix", "-t", "--target-directory"))


def _redirect_targets(e) -> List[str]:
    out = []
    for op, target in e.redirects:
        if ">" not in op:
            continue                                     # <, <<<
        if op.endswith("&") and re.fullmatch(r"[0-9]+|-", target):
            continue                                     # 2>&1: a duplication, not a file
        if target.lower() in NULL_TARGETS:
            continue
        out.append(target)
    return out


def _sed_files(argv) -> List[str]:
    if not any(_SED_INPLACE.fullmatch(w) for w in argv):
        return []
    words, script_given, k = [], False, 0
    while k < len(argv):
        w = argv[k]
        if w in _SED_VALUED:
            script_given = script_given or w in ("-e", "-f", "--expression", "--file")
            k += 2
            continue
        if w.startswith(("--expression=", "--file=")) or (w.startswith(("-e", "-f")) and len(w) > 2):
            script_given = True
        elif not (w.startswith("-") and len(w) > 1):
            words.append(w)
        k += 1
    return words if script_given else words[1:]


# S-SED-CRLF (P8-SED): Git for Windows' sed (GNU 4.9, MSYS) opens files in TEXT mode, so an in-place edit without
# -b/--binary rewrites EVERY CRLF line ending of the file to LF -- untouched lines, and a run that matches nothing, too
# (MEASURED on scratch files). -b keeps them and is a no-op on an LF file. GNU getopt permutes, so -b counts anywhere
# before `--`. In a short cluster -i and -e/-f/-l take the rest of the word as their value: `-bi` is binary, `-ib` is
# in-place with the backup suffix "b" (MEASURED: CRLF lost, a `...b` backup written), `-es/b/x/` is a script.
_SED_ATTACHED = frozenset("iefl")


def sed_binary(argv) -> bool:
    k = 0
    while k < len(argv):
        w = argv[k]
        if w == "--":
            return False
        if w == "--binary":
            return True
        if w in _SED_VALUED:
            k += 2
            continue
        if w.startswith("-") and not w.startswith("--"):
            for c in w[1:]:
                if c == "b":
                    return True
                if c in _SED_ATTACHED:
                    break
        k += 1
    return False


def _sed_scripts(argv) -> List[str]:
    """The sed scripts given: every -e / --expression value, else the first operand (a -f file is not read)."""
    scripts, given, k = [], False, 0
    while k < len(argv):
        w = argv[k]
        if w in ("-e", "--expression") and k + 1 < len(argv):
            scripts.append(argv[k + 1])
            given = True
            k += 2
            continue
        if w in ("-f", "--file"):
            given = True
            k += 2
            continue
        if w in _SED_VALUED:
            k += 2
            continue
        if w.startswith("--expression="):
            scripts.append(w.split("=", 1)[1])
            given = True
        elif w.startswith("--file=") or (w.startswith("-f") and len(w) > 2):
            given = True
        elif w.startswith("-e") and len(w) > 2:
            scripts.append(w[2:])
            given = True
        elif not (w.startswith("-") and len(w) > 1) and not given:
            return [w]
        k += 1
    return scripts


def _sed_part(s: str, i: int, d: str) -> int:
    """Index of the next unescaped delimiter d at or after i, or -1."""
    while i < len(s):
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == d:
            return i
        i += 1
    return -1


def sed_replacements(script: str) -> List[str]:
    """The replacement of every `s<d>pattern<d>replacement<d>` command in a sed script. Linear: each command is
    consumed once, and an unterminated one ends the scan."""
    out, i, n = [], 0, len(script)
    while i < n - 1:
        if script[i] == "s" and (i == 0 or not (script[i - 1].isalpha() or script[i - 1] == "\\")):
            d = script[i + 1]
            if d not in "\\\n" and not d.isalnum() and not d.isspace():
                j = _sed_part(script, i + 2, d)
                k = _sed_part(script, j + 1, d) if j >= 0 else -1
                if k < 0:
                    break
                out.append(script[j + 1:k])
                i = k + 1
                continue
        i += 1
    return out


# A backslash PATH as sed receives it: a drive path (`C:\x`) or two backslash separators between names
# (`Data\Scripts\x`). Each backslash is SINGLE: sed reads `\\` as a literal backslash (correct), and `\G`, `\U`, `\x..`
# as escapes (the mangling). Matched on a shape where each single-backslash escape is \x00 and `\\` is `/`.
_SED_WIN = re.compile(r"[A-Za-z]:\x00|(?<![A-Za-z0-9_])[A-Za-z0-9_]{2,}\x00[A-Za-z0-9_][A-Za-z0-9_ .-]*\x00[A-Za-z0-9_]")
SED_ZONES = ("install", "config", "mo2")


def sed_win_replacement(script: str) -> bool:
    for r in sed_replacements(script):
        shape, i = [], 0
        while i < len(r):
            if r[i] == "\\" and i + 1 < len(r):
                shape.append("/" if r[i + 1] == "\\" else "\x00" + r[i + 1])
                i += 2
                continue
            shape.append(r[i])
            i += 1
        if _SED_WIN.search("".join(shape)):
            return True
    return False


# ------------------------------------------------------------------------------- other byte writers (routes red team)
# Programs that put bytes into a file NAMED on their command line without being a text-write primitive: downloaders,
# decoders, archivers, truncators, in-place editors. Each writes its OWN format (an HTTP body, a zip, a tar, zeros)
# into the name it is given, so onto a plugin or archive name it is the same corruption S-WRITE-PLUGIN denies.
_URL = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
WEB_GET = frozenset(("invoke-webrequest", "iwr", "invoke-restmethod", "irm"))
_CERTUTIL_WRITES = ("-urlcache", "-decode", "-decodehex", "-encode", "-encodehex")


def _url_name(url: str) -> str:
    """The file name curl -O / wget give a download: the URL path's last component."""
    path = re.sub(r"[?#].*$", "", url.split("://", 1)[-1])
    return path.rsplit("/", 1)[-1] if "/" in path else ""


def _opt_values(argv, longs, shorts) -> List[str]:
    """Values of `--long X`, `--long=X`, `-s X` and a short cluster ending in s (`-sLo X`, `-oX`)."""
    out, k = [], 0
    while k < len(argv):
        w = argv[k]
        if w in longs or w in shorts:
            if k + 1 < len(argv):
                out.append(argv[k + 1])
            k += 2
            continue
        if any(w.startswith(lg + "=") for lg in longs):
            out.append(w.split("=", 1)[1])
        elif w.startswith("-") and not w.startswith("--") and len(w) > 2:
            for s in shorts:
                c = s[1:]
                if w.endswith(c) and k + 1 < len(argv):
                    out.append(argv[k + 1])
                    k += 1
                    break
                if w[1:2] == c:
                    out.append(w[2:])
                    break
        k += 1
    return out


def _has_flag(argv, longs, short_letter, valued_letters="") -> bool:
    """`--long`, or `short_letter` in a short cluster before any letter that takes a value (`-XPOST` is -X POST)."""
    for w in argv:
        if w in longs:
            return True
        if w.startswith("-") and not w.startswith("--"):
            for c in w[1:]:
                if c == short_letter:
                    return True
                if c in valued_letters:
                    break
    return False


_CURL_VALUED = "XHdubceAFTwxmrEKoCYyzQPtUD"


def _download_names(argv, outdir_longs, outdir_shorts) -> List[str]:
    """curl -O / wget without -O: each URL's own name, under the output directory when one is given."""
    names = [n for n in (_url_name(u) for u in argv if _URL.fullmatch(u)) if n]
    dirs = _opt_values(argv, outdir_longs, outdir_shorts)
    return [(dirs[-1].rstrip("/\\") + "/" + n) if dirs else n for n in names]


def _first_operand(argv, valued=()) -> List[str]:
    k = 0
    while k < len(argv):
        w = argv[k]
        if w in valued:
            k += 2
            continue
        if not (w.startswith("-") and len(w) > 1):
            return [w]
        k += 1
    return []


def tool_write_targets(e) -> List[str]:
    p, argv = e.program, e.argv
    if p in WEB_GET or (p in ("curl", "wget") and e.dialect == "powershell" and ps_param(e, "outfile")):
        return list(ps_param(e, "outfile"))            # PowerShell 5's curl/wget are Invoke-WebRequest
    if p == "curl":
        out = _opt_values(argv, ("--output",), ("-o",))
        if _has_flag(argv, ("--remote-name", "--remote-name-all"), "O", _CURL_VALUED):
            out += _download_names(argv, ("--output-dir",), ())
        return out
    if p == "wget":
        out = _opt_values(argv, ("--output-document",), ("-O",))
        if out:
            return [w for w in out if w != "-"]
        return _download_names(argv, ("--directory-prefix",), ("-P",))
    if p == "start-bitstransfer":
        return list(ps_param(e, "destination", 1, before=(("source",),)))
    if p == "certutil" and any(w.lower() in _CERTUTIL_WRITES for w in argv):
        ops = [w for w in argv if not w.startswith("-")]
        return ops[-1:]
    if p == "bitsadmin" and any(w.lower() in ("/transfer", "/addfile") for w in argv):
        return list(argv[-1:])
    if p == "truncate":
        return _plain_operands_valued(argv, ("-s", "--size", "-r", "--reference"))
    if p == "perl" and any(re.fullmatch(r"-[a-hj-zA-Z]*i\S*", w) for w in argv):
        return _plain_operands_valued(argv, ("-e", "-E", "-M", "-I", "-m"))[0 if _perl_code_given(argv) else 1:]
    if p == "fsutil" and argv[:1] == ("file",):
        return [w for w in argv[2:] if not w.startswith("-")]
    if p == "openssl":
        return _opt_values(argv, ("-out",), ())
    if p in ("7z", "7za", "7zr", "7z.exe") and argv and argv[0].lower() in ("a", "u", "d", "rn"):
        return _first_operand(argv[1:])
    if p == "zip":
        return _first_operand(argv, ("-b", "-t", "-tt", "-n", "-x", "-i"))
    if p == "tar" and _tar_creates(argv):
        return _opt_values(argv, ("--file",), ("-f",)) or _tar_bundled_file(argv)
    if p == "compress-archive":
        return list(ps_param(e, "destinationpath", 1, before=(("path", "literalpath"),)))
    if p in ("export-csv", "export-clixml"):
        return list(ps_param(e, "path", 0)) + list(ps_param(e, "literalpath"))
    return []


_RSYNC_VALUED = ("--exclude", "--include", "--filter", "-f", "-e", "--rsh", "--files-from", "--exclude-from", "--include-from",
                 "--link-dest", "--compare-dest", "--copy-dest", "-T", "--temp-dir", "-B", "--block-size", "--backup-dir",
                 "--partial-dir", "--suffix", "--log-file", "--password-file", "--port", "--bwlimit", "--timeout",
                 "--max-size", "--min-size", "--rsync-path", "--chmod", "--chown")
_SCP_VALUED = ("-P", "-i", "-o", "-F", "-l", "-c", "-J", "-S", "-D", "-X")     # to rsync, -P -i -o -F -l are plain switches


def _plain_operands_valued(argv, valued) -> List[str]:
    out, k = [], 0
    while k < len(argv):
        w = argv[k]
        if w in valued:
            k += 2
            continue
        if not (w.startswith("-") and len(w) > 1):
            out.append(w)
        k += 1
    return out


def _remote_copy_operands(e) -> List[str]:
    return _plain_operands_valued(e.argv, _SCP_VALUED if e.program == "scp" else _RSYNC_VALUED)


def _perl_code_given(argv) -> bool:
    return any(w in ("-e", "-E") for w in argv)


def _tar_creates(argv) -> bool:
    first = argv[0] if argv else ""
    if first and not first.startswith("-") and re.fullmatch(r"[A-Za-z]+", first):
        return bool(set(first) & set("cruA"))                # old-style bundled `tar cf x.tar ...`
    return any(w in ("--create", "--append", "--update", "--concatenate") or
               (w.startswith("-") and not w.startswith("--") and set(w[1:]) & set("cruA")) for w in argv)


def _tar_bundled_file(argv) -> List[str]:
    first = argv[0] if argv else ""
    if first and not first.startswith("-") and "f" in first and len(argv) > 1:
        return [argv[1]]
    return []


# PowerShell .NET calls that write a file named in their FIRST string argument... (parsed as text: shellparse does not
# model method calls): WebClient.DownloadFile(url, PATH), FileStream(PATH, ...), [IO.File]::Open/Create/OpenWrite.
_PS_METHOD_WRITE = re.compile(
    r"""(?:\.DownloadFile(?:Async)?\(\s*[^,()]+,\s*|FileStream\s*\(?\s*|::(?:Open|Create|OpenWrite)\(\s*)"""
    r"""['"]([^'"\n]{1,400})['"]""", re.IGNORECASE)
# Interpreter code that writes a file NAMED by a string literal: Python open(..., 'w'/'a'/'x'/'+'), Path(...).write_*,
# os.truncate; Node fs.writeFile*/appendFile*/truncate*/createWriteStream. Only a literal name is read.
_LIT = r"""\s*[rRbBuUfF]{0,2}(['"])([^'"\n]{1,400})\1"""
_INTERP_WRITE = (
    re.compile(r"\bopen\(" + _LIT + r"""\s*,\s*(?:mode\s*=\s*)?[rRbBuU]{0,2}['"][rbt]*[wax+]""", re.IGNORECASE),
    re.compile(r"\bPath\(" + _LIT + r"\s*\)\s*\.\s*(?:write_bytes|write_text|open\(\s*['\"][rbt]*[wax+])"),
    re.compile(r"\bos\.truncate\(" + _LIT),
    re.compile(r"\b(?:writeFile|appendFile|truncate|createWriteStream)(?:Sync)?\(" + _LIT),
)


_INTERP_TWO_LITS = re.compile(r"\b(?:copyFile|rename|cp)(?:Sync)?\(" + _LIT + r"\s*,\s*(['\"])([^'\"\n]{1,400})\3")


def text_write_targets_ps(text: str) -> List[str]:
    return [m.group(1) for m in _PS_METHOD_WRITE.finditer(text)]


class _CopyWord(str):
    """A name a copy or move writes or takes away: judged as cp and mv are, not as a raw write of the bytes."""

    def __new__(cls, word, source="", moved=False, tree=False, removed=False):
        self = str.__new__(cls, word)
        self.source, self.moved, self.tree, self.removed = source or "", moved, tree, removed
        return self


# Python this guard cannot parse: a newer syntax than the guard's own Python (a nested f-string quote), or a shell
# variable in the code. Refusing all of it refused 67 of 114,631 real commands, many of them read-only one-liners
# (2026-11-04). This is a text scan for the common spellings of a change, an accident check and no proof: review found
# contrived code that passes it. It keeps the refusal on anything that names a way to change a file, run a program or
# reach a name indirectly, in a comment or string too: a false hit costs only the refusal this code had anyway.
_READ_ONLY_IMPORTS = frozenset((
    "base64", "binascii", "collections", "copy", "csv", "dataclasses", "datetime", "decimal", "difflib",
    "enum", "fnmatch", "fractions", "functools", "glob", "hashlib", "html", "itertools", "json", "locale", "math",
    "os", "os.path", "pathlib", "platform", "pprint", "random", "re", "statistics", "string", "struct",
    "sys", "textwrap", "time", "typing", "unicodedata", "xml.etree", "xml.etree.ElementTree"))
# `from <module> import *` names: built below the change tables, from every module-level call they count (#26
# round 8 review: a hand-kept list missed io.FileIO, gzip/tarfile/zipfile classes, os.link, shutil.unpack_archive).
_READ_OPEN = re.compile(
    r"""(?<![.\w])open\(\s*(?:[rRbBuU]{0,2}"[^"\n]*"|[rRbBuU]{0,2}'[^'\n]*'|[\w.\[\]]+)\s*"""
    r"""(?:,\s*(?:mode\s*=\s*)?['"](?:r|rb|rt|br|tr)['"])?\s*"""
    r"""(?:,\s*(?:encoding|errors|newline)\s*=\s*(?:"[^"\n]*"|'[^'\n]*'|[\w.]+))*\s*\)""")
_OS_READS = ("environ|path|getcwd|sep|linesep|listdir|scandir|walk|stat|lstat|access|getenv|getpid|cpu_count|fspath|"
             "name|pathsep|curdir|pardir")
_MAY_CHANGE = re.compile(
    r"""\bopen\b"""                                    # what is left once the plain reads are taken out
    r"""|\bos\b(?!\s*\.\s*(?:""" + _OS_READS + r""")\b)"""
    r"""|\b(?:write|write_text|write_bytes|writelines|unlink|rmdir|mkdir|makedirs|touch|rename|chmod|symlink_to|"""
    r"""hardlink_to|truncate|exec|eval|getattr|setattr|delattr|globals|locals|vars|breakpoint)\b"""
    r"""|(?<!re\.)\bcompile\b"""
    r"""|\b(?:shutil|subprocess|Popen|importlib|ctypes|modules|builtins|f_builtins|io)\b|\b_[A-Za-z_]\w*"""
    r"""|\$\(|`|%\w+%""")                              # a shell command or a cmd variable inside the code
_IMPORT = re.compile(r"""\bfrom\s+([\w.]+)\s*import\b([^\n;]*)|\bimport\b([^\n;]*)""")
_STRING = re.compile(r"""[rRbBuUfF]{0,2}(?:"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')""")


def _unparsed_may_change_files(code: str) -> bool:
    # As Python and the shell will read it: names are NFKC-normalised. A backslash at a line's end is a space to Python
    # between two tokens, and nothing at all to bash in an unquoted heredoc.
    text = unicodedata.normalize("NFKC", code).replace("\r\n", "\n")
    return any(_names_a_change(t) for t in (text.replace("\\\n", " "), text.replace("\\\n", "")))


def _names_a_change(text: str) -> bool:
    if re.fullmatch(r"""\s*["']?\$\{?\w+\}?["']?\s*""", text):
        return True
    # A shell variable outside a string can hold any code. Inside one it is only text, as in code this guard can read.
    if "$" in _STRING.sub(" ", text):
        return True
    for frm, what, plain in _IMPORT.findall(text):
        if frm:
            # `from os import remove` takes the name past the os. check, so only the os.path submodule may be named.
            if (frm not in _READ_ONLY_IMPORTS or frm == "os" or re.search(r"[*(]|\bas\b", what)
                    or _MAY_CHANGE.search(what)):
                return True
        else:
            for part in plain.split(","):
                if part.strip() not in _READ_ONLY_IMPORTS:     # `import os as o` is not in the list: o.remove is unread
                    return True
    text = _READ_OPEN.sub(" ", _IMPORT.sub(" ", text))
    if _MAY_CHANGE.search(text):
        return True
    return bool(re.search(r"\b(?:Path|pathlib)\b", text) and re.search(r"\.\s*replace\b", text))   # Path.replace moves a file


def _python_option_i(argv) -> bool:
    """-i among python's own options: not inside -Wignore, not past the program it runs."""
    return "i" in shellparse.python_options(argv)[0]


def _runs_past_a_syntax_error(e, code: str) -> bool:
    """`python -i` carries on after a syntax error, so code it cannot compile still runs line by line."""
    if not shellparse._PYTHON.fullmatch(e.program) or not _python_option_i(e.argv):
        return False
    try:
        python_nodes(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return True
    return False


def _python_write_targets(code: str) -> List[str]:
    """Read literal Python write arguments as Python, without running any code.

    Regex captures are source spellings: a doubled backslash is one backslash
    in the filename. Keep this decoder separate from JavaScript and PowerShell.
    Dynamic expressions remain outside the literal-only analysis.
    """
    if len(code) > 1024 * 1024:
        raise ValueError("Python literal write scope exceeds the analysis limit")
    try:
        nodes = python_nodes(code)
    except SyntaxError:
        if not _unparsed_may_change_files(code):
            return []
        # The invoked Python can have a different syntax version from this guard.
        raise ValueError("Python literal write scope could not be parsed") from None
    except (RecursionError, MemoryError):
        raise ValueError("Python literal write scope could not be parsed") from None

    def literal(node):
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def writing(call, index):
        mode = next((literal(k.value) for k in call.keywords if k.arg == "mode"),
                    literal(call.args[index]) if len(call.args) > index else "r")
        return isinstance(mode, str) and any(c in mode for c in "wax+")

    # What `import x as y` / `from x import y as z` call things (review round 3: `from shutil import move` was no one's).
    modules, names = {}, {}
    for node in nodes:
        if isinstance(node, ast.Import):
            modules.update({a.asname or a.name: a.name for a in node.names})
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.update({a.asname or a.name: node.module + "." + a.name for a in node.names})
            if any(a.name == "*" for a in node.names):        # `from os import *` brings these in by their own names
                names.update({n: node.module + "." + n for n in _STAR_NAMES.get(node.module, ())})

    def path_argument(node):
        if not isinstance(node, ast.Call) or not node.args or node.keywords:
            return None
        func = node.func
        # A bare `Path` is read as pathlib's whatever its import: `from pathlib import *` names no name at all.
        bare = isinstance(func, ast.Name) and (func.id == "Path" or names.get(func.id) == "pathlib.Path")
        dotted = (isinstance(func, ast.Attribute) and func.attr == "Path" and
                  isinstance(func.value, ast.Name) and modules.get(func.value.id, func.value.id) == "pathlib")
        if not bare and not dotted:
            return None
        parts = [literal(arg) for arg in node.args]
        return None if any(part is None for part in parts) else str(Path(*parts))

    out = []
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        func, name = node.func, ""
        if isinstance(func, ast.Name):
            name = names.get(func.id, func.id)
        elif isinstance(func, ast.Attribute):
            parent = path_argument(func.value)
            if parent is not None and (func.attr in ("write_bytes", "write_text") or
                                       func.attr == "open" and writing(node, 0)):
                out.append(parent)
            if parent is not None and func.attr == "unlink":
                out.append(_CopyWord(parent, moved=True, removed=True))
            if parent is not None and func.attr in ("rename", "replace"):     # Path(x).rename(y): both change
                out.append(_CopyWord(parent, moved=True))
                dest = (literal(node.args[0]) or path_argument(node.args[0])) if node.args else None
                out += [_CopyWord(dest, parent)] if dest else []
            if isinstance(func.value, ast.Name) and modules.get(func.value.id, func.value.id) in ("io", "builtins", "os",
                                                                                                  "shutil", "send2trash"):
                name = modules.get(func.value.id, func.value.id) + "." + func.attr
        # Release review 2026-11-02: moves and copies by literal name. The destination is written; a move's source goes.
        changes = {"os.rename": 2, "os.replace": 2, "os.renames": 2, "shutil.move": 2, "shutil.copy": 1,
                   "shutil.copy2": 1, "shutil.copyfile": 1, "shutil.copytree": 1}
        if name in changes:
            given = {k.arg: k.value for k in node.keywords}
            first = node.args[0] if node.args else given.get("src")
            second = node.args[1] if len(node.args) > 1 else given.get("dst")
            dest, source = (literal(second) if second is not None else None), (literal(first) if first is not None else None)
            out += [_CopyWord(dest, source or "", tree=name == "shutil.copytree")] if dest else []
            out += [_CopyWord(source, moved=True)] if source and changes[name] == 2 else []
            continue
        if name in ("os.remove", "os.unlink", "send2trash.send2trash"):
            given = {k.arg: k.value for k in node.keywords}
            first = node.args[0] if node.args else given.get("path", given.get("paths"))
            items = first.elts if isinstance(first, (ast.List, ast.Tuple, ast.Set)) else [first] if first is not None else []
            words = [w for w in (literal(i) or path_argument(i) for i in items) if w]
            if words:
                out += [_CopyWord(w, moved=True, removed=True) for w in words]   # read for saves; deletes.py has the rest
                continue
        keyword = "path" if name == "os.truncate" else "file"
        argument = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == keyword), None)
        value = literal(argument)
        if value is not None and (name == "os.truncate" or
                name in ("open", "io.open", "builtins.open") and writing(node, 1)):
            out.append(value)
    return out


# Issue #26: a Python write whose target is computed (`for p in Path(S).glob('*.ess'): p.write_bytes(b'')`) names no
# file, so the literal reader above returns nothing for it and the save policy never saw the saves it changes.
# These are the calls that write (or move a file onto) their target; deletes are read by deletes.py (S-DEL-CONFIG).
# The save_targets entry for such a write: saves.count reads it as a save it cannot list (S-SAVE-UNKNOWN), with words
# that say what to do instead.
COMPUTED_SAVE = "<computed by inline Python>"
COMPUTED_SAVE_WHY = "inline Python computes the target; name saves literally, or split read and write"
_PY_WRITE_METHODS = frozenset(("write_bytes", "write_text", "touch", "symlink_to", "hardlink_to"))
# name -> the (position, keyword) arguments a call writes or takes away. A move counts its source too: the save goes.
_PY_WRITE_FUNCS = {
    "open": ((0, "file"),), "io.open": ((0, "file"),), "builtins.open": ((0, "file"),), "io.FileIO": ((0, "file"),),
    "zipfile.ZipFile": ((0, "file"),), "tarfile.TarFile": ((0, "name"),), "gzip.GzipFile": ((0, "filename"),),
    "bz2.BZ2File": ((0, "filename"),), "lzma.LZMAFile": ((0, "filename"),), "os.open": ((0, "path"),), "os.truncate": ((0, "path"),),
    "shutil.copy": ((1, "dst"),), "shutil.copy2": ((1, "dst"),), "shutil.copyfile": ((1, "dst"),),
    "shutil.copytree": ((1, "dst"),), "os.link": ((1, "dst"),), "os.symlink": ((1, "dst"),),
    "shutil.move": ((0, "src"), (1, "dst")), "os.rename": ((0, "src"), (1, "dst")),
    "os.replace": ((0, "src"), (1, "dst")), "os.renames": ((0, "old"), (1, "new")),
}
_PY_MODE_AT = {"open": 1, "io.open": 1, "builtins.open": 1, "io.FileIO": 1, "zipfile.ZipFile": 1,
               "tarfile.TarFile": 1, "gzip.GzipFile": 1, "bz2.BZ2File": 1, "lzma.LZMAFile": 1}   # absent = read


class ComputedWrites(NamedTuple):
    """What python_computed_writes found (#26).
    computed: a write, move, link or extraction whose target is not a direct literal, so it can land anywhere.
    literals: the string literals that can name where it lands (see _location_literals), plus every extraction's
              literal destination (the literal reader does not read extraction)."""
    computed: bool
    literals: Tuple[str, ...]


_PY_EXTRACT_METHODS = {"extractall": (0, "path"), "extract": (1, "path")}   # ZipFile / TarFile
_PY_EXTRACT_FUNCS = {"shutil.unpack_archive": (1, "extract_dir")}
# Module opens called by name after `from gzip import *` (`gzip.open(...)` by attribute is read below as well).
_PY_MODULE_OPENS = {"gzip.open": ((0, "filename"),), "bz2.open": ((0, "filename"),), "lzma.open": ((0, "filename"),),
                    "tarfile.open": ((0, "name"),)}
# #26 round 8 (user ruling 14:58 covers deletes): a computed delete counts as a change the guard cannot place, so one
# run from inside the settings area (`cd Saves && python -c "[os.remove(f) for f in os.listdir()]"`, allowed in 4.0)
# is refused. A literal os.remove/os.unlink/send2trash/Path.unlink target is still placed (the literal reader returns
# it) and judged by deletes.py; a literal rmtree/rmdir/removedirs target counts as computed.
_PY_DELETE_FUNCS = {"os.remove": ((0, "path"),), "os.unlink": ((0, "path"),), "os.rmdir": ((0, "path"),),
                    "os.removedirs": ((0, "name"),), "shutil.rmtree": ((0, "path"),),
                    "send2trash.send2trash": ((0, "paths"),)}
_PY_DELETE_METHODS = frozenset(("unlink", "rmdir"))       # Path(...).unlink() / .rmdir()


# `nt` and `posix` are the modules os re-exports (round 9 review: `nt.remove(n)` was read as no call at all).
_OS_SPELLINGS = {"nt": "os", "posix": "os"}


def _star_names() -> dict:
    out: dict = {}
    # Module opens are left out: after `from gzip import *` a bare `open(p, 'wb')` reads as builtin open, counted the
    # same way (`from gzip import open as gopen` is read by name below).
    for table in (_PY_WRITE_FUNCS, _PY_EXTRACT_FUNCS, _PY_DELETE_FUNCS):
        for dotted in table:
            module, _, name = dotted.rpartition(".")
            if module and module != "builtins":
                out.setdefault(module, set()).add(name)
    return {module: tuple(sorted(names)) for module, names in out.items()}


_STAR_NAMES = _star_names()
_PY_VALUE_COUNTED = (frozenset(_PY_WRITE_FUNCS) | frozenset(_PY_DELETE_FUNCS) | frozenset(_PY_EXTRACT_FUNCS) |
                     frozenset(_PY_MODULE_OPENS)) - {"open", "io.open", "builtins.open"}


def python_computed_writes(code: str, conservative: bool = True) -> ComputedWrites:
    """Read as Python (ast, never run): is anything written, moved, linked or extracted to a target this guard cannot
    place? Fail closed (user decision 2026-11-09, after three reviews of finer placement): only a direct literal target
    -- a string, or `Path(<literals>)` -- is placed, and _python_write_targets judges it with the full save policy.
    Anything else is computed: a name, a `/` chain, os.path.join, a loop item. With an os.chdir in the code a relative
    literal is computed too (it lands wherever chdir pointed). Archive extraction always counts as computed.

    Counted: `Path` write_bytes/write_text/touch/symlink_to/hardlink_to and `.open` in a writing mode; `.rename(x)` and
    a one-argument `.replace(x)` (a str.replace takes two), both sides; open/io.open/io.FileIO in a writing mode,
    os.open unless os.O_RDONLY, os.truncate, copies onto a destination, links, moves (source and destination), and
    ZipFile/TarFile extractall/extract and shutil.unpack_archive. A module's `.open(file, mode)` (gzip, tarfile,
    codecs) reads its mode from the second argument; a mode chosen at run time may write. Not read (known limits):
    getattr and other string-named access (operator.methodcaller, `os.__dict__[...]`), private helpers
    (shutil._rmtree_unsafe), exec/eval of code strings, __import__, subprocess-launched copies. A counted function or
    write/delete method handed on as a value (`map(os.remove, ...)`, `rep = os.replace`, `[p.unlink for p in ...]`)
    counts as computed; `nt`/`posix` are read as os.
    Unparseable code returns nothing (scan's unparsed-code refusal)."""
    try:
        nodes = python_nodes(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return ComputedWrites(False, ())
    modules, names = {}, {}
    for node in nodes:
        if isinstance(node, ast.Import):
            modules.update({a.asname or a.name: _OS_SPELLINGS.get(a.name, a.name) for a in node.names})
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            module = _OS_SPELLINGS.get(node.module, node.module)          # `from nt import unlink` is os.unlink
            names.update({a.asname or a.name: module + "." + a.name for a in node.names})
            if any(a.name == "*" for a in node.names):        # `from os import *` (review round 5)
                names.update({n: module + "." + n for n in _STAR_NAMES.get(module, ())})
    try:                                                  # what the literal reader itself judges (round 6)
        emitted = {os.path.normcase(str(w)) for w in _python_write_targets(code)}
    except ValueError:
        emitted = set()
    moves_cwd = any(isinstance(n, ast.Call) and (
        isinstance(n.func, ast.Attribute) and n.func.attr == "chdir" or
        isinstance(n.func, ast.Name) and names.get(n.func.id, n.func.id) in ("os.chdir", "chdir")) for n in nodes)

    def direct(node):
        """A string, or Path(<strings>): _python_write_targets reads these. Relative ones are not, after a chdir."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
        elif isinstance(node, ast.Call) and node.args and not node.keywords and all(
                isinstance(a, ast.Constant) and isinstance(a.value, str) for a in node.args):
            text = node.args[0].value                     # Path(<literals>); anything else is not emitted
            value = str(Path(*[a.value for a in node.args]))
        else:
            return False
        if isinstance(node, ast.Constant):
            value = text
        # Placed only when the literal reader returned this very literal: a keyword or mode it does not read
        # (`os.renames(old=, new=)`, `Path.rename(target=)`) is computed (#26 round 5 review).
        if os.path.normcase(value) not in emitted:
            return False
        return not moves_cwd or bool(re.match(r"^(?:[A-Za-z]:)?[/\\]", text))

    computed = False
    extracted: List[str] = []

    def target(node):
        nonlocal computed
        if node is not None and not direct(node):
            computed = True

    def extraction(node):
        nonlocal computed
        computed = True                                   # no destination = the cwd; a literal one is still a place
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            extracted.append(node.value)                  # a computed destination's literals: _location_literals

    def argument(call, index, keyword):
        if len(call.args) > index:
            return call.args[index]
        return next((k.value for k in call.keywords if k.arg == keyword), None)

    def mode_writes(call, index):
        mode = argument(call, index, "mode")
        if mode is None:
            return False
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            return any(c in mode.value for c in "wax+")
        return True                                       # a mode chosen at run time may write

    def os_open_writes(call):
        flags = argument(call, 1, "flags")
        # Only the real os.O_RDONLY (or 0): `class K: O_RDONLY = 0x301` is write+create+truncate (list review 2), and
        # nothing may assign an O_RDONLY anywhere (`os.O_RDONLY = 0x301`, `O_RDONLY = 0x301`; list review 3).
        if any(isinstance(n, (ast.Name, ast.Attribute)) and isinstance(n.ctx, ast.Store) and
               (getattr(n, "id", None) == "O_RDONLY" or getattr(n, "attr", None) == "O_RDONLY") for n in nodes):
            return not (isinstance(flags, ast.Constant) and flags.value == 0)
        # (an `X.O_RDONLY` of another module is an unknown value: the conservative scan refuses it)
        read_only = (isinstance(flags, ast.Attribute) and flags.attr == "O_RDONLY" or
                     isinstance(flags, ast.Name) and names.get(flags.id) == "os.O_RDONLY" or
                     isinstance(flags, ast.Constant) and flags.value == 0)
        return not read_only

    def imported(node):
        return isinstance(node, ast.Name) and (node.id in modules or node.id in names)

    # A counted function handed on as a value (`map(os.remove, os.listdir())`, `list(map(Path.unlink, ...))`) changes
    # targets this guard never sees: computed (round 8 review). Builtin open defaults to reading, so it is left out.
    called = {id(n.func) for n in nodes if isinstance(n, ast.Call)}
    for node in nodes:
        if id(node) in called or not isinstance(getattr(node, "ctx", None), ast.Load):
            continue
        if isinstance(node, ast.Attribute) and node.attr in _PY_DELETE_METHODS | _PY_WRITE_METHODS:
            computed = True             # map(Path.unlink, ...), [p.unlink for p in ...]: any receiver (round 9 review)
            continue
        if isinstance(node, ast.Name):
            value = names.get(node.id, node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            value = modules.get(node.value.id, node.value.id) + "." + node.attr
        else:
            continue
        if value in _PY_VALUE_COUNTED:
            computed = True
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            receiver_is_module = isinstance(f.value, ast.Name) and modules.get(f.value.id, f.value.id) in (
                "io", "builtins", "os", "shutil")
            if f.attr in _PY_WRITE_METHODS and not receiver_is_module:
                target(f.value)
            if f.attr == "open" and not receiver_is_module:
                # gzip.open(file, mode), and any receiver given two arguments (`__builtins__.open(p, 'w')`, a module
                # reached through sys.modules): the file is the first argument, the mode the second (safe-list review).
                if imported(f.value) or len(node.args) >= 2 or any(k.arg in ("file", "filename", "name")
                                                                   for k in node.keywords):
                    if mode_writes(node, 1):
                        target(argument(node, 0, "file") or argument(node, 0, "filename") or argument(node, 0, "name"))
                elif mode_writes(node, 0):                # Path(...).open(mode)
                    target(f.value)
            moving = f.attr == "rename" or f.attr == "replace" and (
                len(node.args) == 1 and not node.keywords or not node.args and [k.arg for k in node.keywords] == ["target"])
            if moving and not receiver_is_module and not imported(f.value):
                target(f.value)
                target(node.args[0] if node.args else argument(node, 0, "target"))
            if f.attr in _PY_EXTRACT_METHODS and not receiver_is_module:
                extraction(argument(node, *_PY_EXTRACT_METHODS[f.attr]))
            if f.attr in _PY_DELETE_METHODS and not receiver_is_module:
                # p.unlink() / p.unlink(True) / p.rmdir() (round 8 review). An unbound Path.unlink(p) has the class as
                # its receiver, never a literal, so it is computed too. os.rmdir(x) is read by name below.
                target(f.value)
        name = ""
        if isinstance(f, ast.Name):
            name = names.get(f.id, f.id)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
            name = modules.get(f.value.id, f.value.id) + "." + f.attr
        if name in _PY_EXTRACT_FUNCS:
            extraction(argument(node, *_PY_EXTRACT_FUNCS[name]))
            continue
        if name in _PY_DELETE_FUNCS:
            for index, keyword in _PY_DELETE_FUNCS[name]:
                given = argument(node, index, keyword)
                # send2trash([...]): each literal is placed on its own, as the literal reader reads them
                for item in (given.elts if isinstance(given, (ast.List, ast.Tuple, ast.Set)) else [given]):
                    target(item)
            continue
        if name in _PY_MODULE_OPENS and isinstance(f, ast.Name):
            if mode_writes(node, 1):                      # by attribute it is the `.open` branch above
                for index, keyword in _PY_MODULE_OPENS[name]:
                    target(argument(node, index, keyword))
            continue
        if name not in _PY_WRITE_FUNCS:
            continue
        if name in _PY_MODE_AT and not mode_writes(node, _PY_MODE_AT[name]):
            continue
        if name == "os.open" and not os_open_writes(node):
            continue
        for index, keyword in _PY_WRITE_FUNCS[name]:
            target(argument(node, index, keyword))
    # #26 conservative scan (user decision 2026-11-10, after six review rounds each found another spelling of a change
    # call: `shutil.os.remove`, `p.rename` passed as a value, ...): the named calls above are not the whole set. Every
    # other call must be a known read; a call this guard does not know counts as computed (fail closed by construction).
    if not computed and conservative:                   # off only in tests of the named layer above
        try:
            computed = _unread_call(nodes, modules, names)
        except RecursionError:                            # a 2000-deep attribute chain: unreadable, so a possible change
            computed = True
    return ComputedWrites(computed, tuple(_location_literals(nodes)) + tuple(extracted))


# The conservative scan's known reads (#26, user decision 2026-11-10; reviewed as a list the same day). A call is known
# when it is one the write logic above already judges, or an EXACT read below. Everything else -- an unknown module
# function, anything deeper than one public name below a read module (`json.codecs.open`, `argparse._os.remove`,
# `ElementTree.ElementTree.write`), a method this list does not name, a call through a subscript, a variable or
# another call -- counts as a possible change. Ambiguous names (`remove`, `add`, `copy`: a list, os, Path in 3.14)
# are deliberately not reads: in code that names a save location they are refused.
_JUDGED_CALLS = (frozenset(_PY_WRITE_FUNCS) | frozenset(_PY_DELETE_FUNCS) | frozenset(_PY_EXTRACT_FUNCS) |
                 frozenset(_PY_MODULE_OPENS) | frozenset(_PY_MODE_AT) |
                 {"codecs.open", "os.chdir", "chdir", "zipfile.ZipFile", "tarfile.TarFile", "tarfile.open"})
_JUDGED_METHODS = _PY_WRITE_METHODS | _PY_DELETE_METHODS | frozenset(("open", "rename", "replace", "extractall",
                                                                         "extract"))
_FILE_WRITES = frozenset(("write", "writelines", "flush", "truncate"))   # judged only on a file a judged open() made
_READ_BUILTINS = frozenset((
    "print", "len", "sorted", "list", "dict", "set", "frozenset", "tuple", "str", "int", "float", "bool", "bytes",
    "bytearray", "memoryview", "range", "enumerate", "zip", "map", "filter", "min", "max", "sum", "any", "all", "abs",
    "round", "repr", "ascii", "isinstance", "issubclass", "hasattr", "type", "iter", "next", "reversed", "format",
    "ord", "chr", "hex", "oct", "bin", "divmod", "hash", "id", "callable", "super", "object", "slice", "complex", "pow",
    "exit", "quit"))
# One public name directly below these is a read (`json.loads`, `hashlib.sha256`, `collections.Counter`); nothing
# deeper and nothing private. None of these modules has a module-level function that writes a file.
_READ_MODULES = frozenset(("json", "hashlib", "re", "math", "struct", "datetime", "time", "os.path", "glob", "fnmatch",
                           "collections", "itertools", "base64", "binascii", "statistics", "textwrap", "string",
                           "difflib", "pprint", "platform", "locale", "decimal", "fractions", "enum",
                           "dataclasses", "unicodedata", "random", "html", "shlex", "uuid", "zlib", "ntpath",
                           "posixpath"))
_READ_FUNCS = frozenset((
    "os.listdir", "os.scandir", "os.walk", "os.stat", "os.lstat", "os.getcwd", "os.getenv", "os.fspath",
    "os.cpu_count", "os.getpid", "os.environ.get", "os.get_terminal_size", "os.urandom", "os.fsencode", "os.fsdecode",
    "os.close", "os.read", "os.fstat", "os.lseek",                          # a descriptor os.open was judged for
    "io.StringIO", "io.BytesIO", "zipfile.is_zipfile", "tarfile.is_tarfile", "shutil.disk_usage",
    "Path", "PurePath", "pathlib.Path", "pathlib.PurePath", "pathlib.PureWindowsPath", "pathlib.PurePosixPath",
    "sys.exit", "sys.getsizeof", "sys.intern", "sys.stdout.write", "sys.stderr.write", "sys.stdout.flush",
    "sys.stderr.flush", "sys.stdout.reconfigure", "sys.stderr.reconfigure", "traceback.print_exc",
    "traceback.format_exc", "functools.reduce", "functools.lru_cache", "functools.cmp_to_key", "functools.wraps",
    "copy.deepcopy", "csv.reader", "csv.DictReader", "datetime.datetime.now", "datetime.datetime.utcnow",
    # a csv writer writes to the file it is handed: that file's open() was judged (or it is stdout)
    "csv.writer", "csv.DictWriter", "sys.stdout.buffer.write", "sys.stdout.buffer.flush",
    # CLI parsing for an audit script; argparse.FileType (it opens files) stays unknown
    "argparse.ArgumentParser",
    "datetime.datetime.fromtimestamp", "datetime.datetime.strptime", "datetime.datetime.fromisoformat",
    "datetime.date.today", "datetime.date.fromtimestamp", "datetime.date.fromisoformat",
    # a save's body is LZ4-compressed: read-only save audits decompress it (priced: 5 such audits, 2026-11-10)
    "lz4.block.decompress", "lz4.frame.decompress"))
_READ_METHODS = frozenset((
    "read", "read_text", "read_bytes", "readline", "readlines", "exists", "is_file", "is_dir", "is_symlink", "iterdir",
    "glob", "rglob", "stat", "lstat", "resolve", "absolute", "relative_to", "is_relative_to", "with_suffix",
    "with_name", "with_stem", "joinpath", "as_posix", "as_uri", "match", "samefile", "expanduser", "home", "cwd",
    "split", "rsplit", "splitlines", "strip", "lstrip", "rstrip", "lower", "upper", "casefold", "title",
    "capitalize", "swapcase", "startswith", "endswith", "join", "format", "format_map", "encode", "decode", "get",
    "keys", "values", "items", "append", "extend", "insert", "pop", "sort", "index", "count", "find", "rfind",
    "partition", "rpartition", "zfill", "center", "ljust", "rjust", "isdigit", "isalpha", "isalnum", "isspace",
    "islower", "isupper", "isnumeric", "isdecimal", "isidentifier", "isprintable", "expandtabs", "translate",
    "maketrans", "removeprefix", "removesuffix", "hexdigest", "digest", "update", "setdefault", "fromkeys",
    "most_common", "namelist", "infolist", "getinfo", "getmembers", "getnames", "getmember", "extractfile", "close",
    "seek", "tell", "fileno", "isatty", "readable", "seekable", "group", "groups", "groupdict", "span", "start", "end",
    "findall", "finditer", "search", "fullmatch", "sub", "subn", "loads", "dumps", "total_seconds", "isoformat",
    "strftime", "timestamp", "date", "time", "difference", "union", "intersection", "issubset", "issuperset",
    "is_absolute", "to_bytes", "from_bytes", "bit_length", "hex", "fromhex", "unpack", "unpack_from", "pack",
    "pack_into", "iter_unpack", "isoweekday", "weekday", "now", "utcnow", "today", "fromtimestamp", "fromisoformat",
    "strptime", "astimezone", "getvalue", "elements", "clear", "reverse", "popleft", "appendleft", "__enter__",
    "__exit__", "is_mount", "owner", "readlink", "samestat", "tobytes", "tolist", "writerow", "writerows",
    "writeheader", "add_argument", "parse_args", "parse_known_args", "add_mutually_exclusive_group",
    "add_argument_group", "print_help", "print_usage", "error"))
_READ_DATA = frozenset((                               # module attributes that are values, not functions
    "os.sep", "os.altsep", "os.linesep", "os.pathsep", "os.curdir", "os.pardir", "os.extsep", "os.devnull",
    "os.environ", "os.name", "os.O_RDONLY", "os.O_BINARY", "os.SEEK_SET", "os.SEEK_CUR", "os.SEEK_END", "sys.argv",
    "sys.stdin", "sys.stdout", "sys.stderr", "sys.platform", "sys.version_info", "sys.path", "sys.executable",
    "sys.maxsize"))
# Method names that change a file when called, refused when handed on as a value (`map(p.chmod, ...)`); a direct call
# of any of them is already refused as an unknown method (list review 2).
_CHANGE_METHOD_VALUES = frozenset(("chmod", "lchmod", "chown", "mkdir", "makedirs", "touch", "copy", "move",
                                   "copy_into", "move_into", "remove", "writestr", "write_text", "write_bytes"))
_EXCEPTION_NAME = r"(?:Error|Exception|Warning|Exit|Interrupt|Iteration)$"   # raise SystemExit(...), StopIteration()

def _unread_call(nodes, modules, names) -> bool:
    """A call this guard neither judges nor knows to be a read (see the tables above)."""
    local = {n.name for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    local |= {t.id for n in nodes if isinstance(n, ast.Assign) and isinstance(n.value, ast.Lambda)
              for t in n.targets if isinstance(t, ast.Name)}
    roots = {k.split(".")[0] for k in modules}

    # Safe-list review: a name the tables trust must mean what its import says. Rebinding one (`json = os`, a
    # parameter or loop variable named `json`, `def print(...)`) and then calling through it is a possible change.
    protected = set(modules) | roots | set(names) | _READ_BUILTINS | {"open", "Path", "PurePath", "__builtins__"}
    binds: dict = {}

    def bind(name):
        binds[name] = binds.get(name, 0) + 1

    for n in nodes:
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bind(n.id)
        elif isinstance(n, ast.arg):
            bind(n.arg)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            for name in n.names:
                bind(name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bind(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bind(n.name)
        elif type(n).__name__ in ("MatchAs", "MatchStar") and getattr(n, "name", None):
            bind(n.name)
    bound = set(binds)
    # A local def or lambda is trusted only when that is its one binding: `def w(): pass; w = open; w(n, 'w')`, a
    # parameter or comprehension variable of the same name, is something else (list review 3).
    local = {name for name in local if binds.get(name, 0) == 1}
    # An import name bound to two different things (`import os as json` beside a decoy `import json` elsewhere): the
    # tables above saw only the last one (list review 2).
    imported = {}
    for n in nodes:
        if isinstance(n, ast.Import):
            for a in n.names:
                imported.setdefault(a.asname or a.name.split(".")[0], set()).add(a.name if a.asname else a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                imported.setdefault(a.asname or a.name, set()).add((n.module or "") + "." + a.name)
    import_names = set(modules) | roots | set(names)
    rebound = (bound & protected) | {k for k, v in imported.items() if len(v) > 1}

    def root_name(node):
        while isinstance(node, ast.Attribute):
            node = node.value
        return node.id if isinstance(node, ast.Name) else None

    # Calling the rebound name itself (`print = ...; print(n)`), or calling through a rebound IMPORT (`json = os;
    # json.remove(n)`). A rebound builtin or variable used only as a method receiver (`hash = sha256(...);
    # hash.hexdigest()`) is an ordinary variable.
    for n in nodes:
        if isinstance(n, ast.Call):
            root = root_name(n.func)
            if root not in rebound:
                continue
            if isinstance(n.func, ast.Name):
                return True
            # through a rebound import, only a known read method: `path.lower()` after `for path in ...` is a str
            if root in import_names and n.func.attr not in _READ_METHODS:
                return True

    # Files a judged open() made: their write()/flush() is judged by that open's mode.
    files = set()
    for n in nodes:
        opened = None
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            opened, alias = n.value, n.targets[0].id
        elif isinstance(n, ast.withitem) and isinstance(n.optional_vars, ast.Name):
            opened, alias = n.context_expr, n.optional_vars.id
        if isinstance(opened, ast.Call) and (isinstance(opened.func, ast.Name) and opened.func.id == "open" or
                                             isinstance(opened.func, ast.Attribute) and opened.func.attr == "open"):
            files.add(alias)

    def dotted(node):
        """`a.b.c` from a module name, or None when the chain does not start at an imported module."""
        if isinstance(node, ast.Name):
            if node.id in modules:
                return modules[node.id]
            if node.id in roots:                          # `import lz4.block` binds `lz4`
                return node.id
            return names.get(node.id) if node.id in names else None
        if isinstance(node, ast.Attribute):
            head = dotted(node.value)
            return head + "." + node.attr if head else None
        return None

    def known(name):
        parts = name.split(".")
        # A module reached through another (`os.path.os.remove`, `shutil.os.remove`, `json.codecs.open`) is never
        # a read: only exactly one name below a read module is (below), and a judged call counts only when the
        # write logic named its module directly (it does not read `shutil.os`).
        data = next((d for d in _READ_DATA if name.startswith(d + ".")), None)
        if data is not None:                              # a method on a module value: `os.sep.join(...)`
            return name in _READ_FUNCS or parts[-1] in _READ_METHODS
        if (name in _JUDGED_CALLS and len(parts) == 2) or name in _READ_FUNCS:
            return True
        module, _, attr = name.rpartition(".")
        return module in _READ_MODULES and bool(attr)    # exactly one name below: `json.codecs.open` is not a read

    def judged_file_write(f):
        receiver = f.value
        if isinstance(receiver, ast.Name) and receiver.id in files:
            return True
        return isinstance(receiver, ast.Call) and (
            isinstance(receiver.func, ast.Name) and receiver.func.id == "open" or
            isinstance(receiver.func, ast.Attribute) and receiver.func.attr == "open")

    # A function handed on as a value is called where this guard cannot see (`map(subprocess.call, ...)`,
    # `map(Path.rename, ...)`): a module function or a judged method referenced but not called must be a known read.
    called = {id(n.func) for n in nodes if isinstance(n, ast.Call)}
    inner = {id(n.value) for n in nodes if isinstance(n, ast.Attribute)}
    for node in nodes:
        if id(node) in called or id(node) in inner or not isinstance(getattr(node, "ctx", None), ast.Load):
            continue
        if isinstance(node, ast.Attribute) and node.attr in _JUDGED_METHODS | _FILE_WRITES | _CHANGE_METHOD_VALUES:
            return True                                   # `map(p.chmod, ...)`: a change method handed on
        name = (dotted(node) if isinstance(node, ast.Attribute) else
                names.get(node.id) if isinstance(node, ast.Name) and node.id not in local else None)
        if isinstance(node, ast.Name) and node.id in _JUDGED_CALLS and node.id not in names and node.id not in local:
            return True                                   # `map(open, ns, ['w'] * n)`: builtin open handed on
        if name is not None and (not known(name) or name in _JUDGED_CALLS) and name not in _READ_DATA:
            # a judged call handed on as a value is not judged: its arguments are never seen (`map(io.open, ...)`)
            return True
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # `os.remove(**{'path': n})`, `open(*[n, 'w'])`: the write logic reads arguments by position and keyword,
        # so an unpacked argument is unread there. Only a known READ may take one (`print(*rows)`) (list review 2).
        if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
            if isinstance(f, ast.Name) and (f.id in local or f.id in _READ_BUILTINS):
                continue
            target_name = dotted(f) if isinstance(f, ast.Attribute) else names.get(f.id) if isinstance(
                f, ast.Name) else None
            if target_name is not None and known(target_name) and target_name not in _JUDGED_CALLS:
                continue
            if target_name is None and isinstance(f, ast.Attribute) and f.attr in _READ_METHODS:
                continue
            return True
        if isinstance(f, ast.Name):
            if f.id in local or f.id in _READ_BUILTINS or re.search(_EXCEPTION_NAME, f.id):
                continue
            if f.id in names:
                if known(names[f.id]):
                    continue
                return True
            if f.id in _JUDGED_CALLS or f.id in _READ_FUNCS:
                continue
            return True                                   # an unknown name: a variable holding any callable
        if isinstance(f, ast.Attribute):
            name = dotted(f)
            if name is not None:                          # a module function, however deep (`shutil.os.remove`)
                if known(name):
                    continue
                return True
            if f.attr in _READ_METHODS or f.attr in _JUDGED_METHODS:
                continue
            if f.attr in _FILE_WRITES and judged_file_write(f):
                continue
            return True
        if isinstance(f, ast.Lambda):
            continue
        return True                                       # a call through a subscript or another call's result
    return False


# A literal outside a path position names a location only when it is shaped like a path: it starts with a drive, a
# separator, `~`, `%`, `$`, `{` or `word/`, and holds no shell separators (a command line held as data is text).
_PATH_SHAPED = re.compile(r"""^(?:[A-Za-z]:[/\\]|[/\\~%${]|\.{1,2}[/\\]|[\w.-]+[/\\])""")
_SHELLISH = re.compile(r"""[;|`]|&&|\n""")
# Calls whose arguments are content or swapped-in text, never a location (endswith's are a filter: kept).
_CONTENT_CALLS = frozenset(("print", "write", "writelines", "write_text", "write_bytes", "dumps", "dump", "log",
                            "debug", "info", "warning", "error", "exception"))


def _location_literals(nodes) -> List[str]:
    """The literals that can name where a computed target lands (#26 round 3, review: the round-2 list of positions
    missed helper functions, defaults, argv fallbacks, tuples and `+=`). Path positions (see _path_literals) count as
    they are; any other literal counts when it is path-shaped. Never: arguments of text methods (but endswith's),
    content and log calls, multi-line strings, text not shaped like a path, and command lines held as data."""
    out = list(_path_literals(nodes))
    skip = set()
    for node in nodes:
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            text_method = isinstance(f, ast.Attribute) and name in _TEXT_METHODS
            if name in ("endswith", "fnmatch", "fnmatchcase", "match_suffix"):
                for arg in node.args:                     # n.endswith(('.ess', '.skse')): a save filter
                    out += [c.value for c in ast.walk(arg) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            elif text_method or name in _CONTENT_CALLS:
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    skip.update(id(c) for c in ast.walk(arg))
        elif isinstance(node, ast.Compare) and any(isinstance(s, ast.Attribute) and s.attr in ("suffix", "suffixes")
                                                    for s in [node.left] + list(node.comparators)):
            out += [c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
    for node in nodes:
        if (isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
                and len(node.value) < 1024 and not _SHELLISH.search(node.value) and _PATH_SHAPED.match(node.value)):
            out.append(node.value)
    return out


# Calls whose arguments are paths. str.replace is not one (its arguments are text an edit swaps), nor print/format.
_PATH_CALLS = frozenset((
    "Path", "PurePath", "PureWindowsPath", "PurePosixPath", "WindowsPath", "PosixPath", "join", "joinpath", "glob",
    "iglob", "rglob", "listdir", "scandir", "walk", "open", "FileIO", "chdir", "expanduser", "expandvars", "abspath",
    "realpath", "normpath", "fspath", "exists", "is_dir", "is_file", "iterdir", "copy", "copy2", "copyfile",
    "copytree", "move", "rename", "remove", "unlink", "truncate", "with_name", "with_suffix", "mkdir", "makedirs"))


_TEXT_METHODS = frozenset(("replace", "format", "count", "find", "rfind", "index", "split", "rsplit", "strip", "lstrip",
                           "rstrip", "startswith", "endswith", "encode", "decode", "upper", "lower", "partition",
                           "removeprefix", "removesuffix", "sub", "subn", "search", "match", "fullmatch", "findall"))


def _path_literals(nodes) -> List[str]:
    """The string literals that can be part of a path (#26 pricing): arguments of a path call, operands of `/`, and
    values assigned to a name or iterated over. Not text: str.replace/print/comparison arguments, docstrings and
    multi-line strings, and dict values (a path kept in a dict and used later is a known limit)."""
    out: List[str] = []
    shallow = _shallow_path_strings(out)

    def strings(node):
        stack = [node]
        while stack:
            n = stack.pop()
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in _TEXT_METHODS):
                stack.append(n.func.value)                # 'C:/x/a.skse'.replace(...): the receiver can be a path,
                continue                                  # the arguments are text the edit swaps in
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                out.append(n.value)
            stack.extend(ast.iter_child_nodes(n))

    for node in nodes:
        if isinstance(node, ast.Call):
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
            if name in _PATH_CALLS:
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    strings(arg)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            strings(node.left)
            strings(node.right)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None and all(
                isinstance(t, ast.Name) for t in (node.targets if isinstance(node, ast.Assign) else [node.target])):
            shallow(node.value)
        elif isinstance(node, (ast.For, ast.comprehension)):
            shallow(node.iter)
    return out


def _shallow_path_strings(out):
    """One-token literals ('Saves', '*.ess') in an assigned, looped-over or concatenated value: `SAVES = 'Saves'` then
    `Path(D) / SAVES`. A path-shaped literal anywhere is read by _location_literals (round 3); this adds the bare
    tokens that only name saves in these positions. A token with whitespace or shell separators is text."""
    def shallow(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if not re.search(r"\s", node.value) and not _SHELLISH.search(node.value):
                out.append(node.value)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Div)):
            shallow(node.left)
            shallow(node.right)
        elif isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for element in node.elts:
                shallow(element)
    return shallow


def names_save_location(text: str) -> bool:
    """A string literal that spells part of a save path without resolving to one by itself (#26 review): a path
    component `Saves`, or a name or pattern ending .ess/.skse. Built paths (`os.path.join(D, 'Saves')`,
    `Path.home() / ... / 'Saves'`, `glob('*.ess')`) carry one of these."""
    low = text.replace("\\", "/").casefold()
    return low.endswith(SAVE_EXTS) or "saves" in [part.strip() for part in low.split("/")]


# Round 8 review: a word boundary, not a list of separators (`cd "$D"/Saves&& ...`), and `My\ Games` / `My%20Games`.
_SAVES_WORD = r"(?<![\w.-])saves(?![\w-])"
_SAVE_NAME = r"\.(?:ess|skse)\b"
_MY_GAMES = r"my(?:\s|%20)+games"            # `My\ Games` is read through _bash_unquoted
_SAVE_DIRS_VAR = r"SKYRIM_SAVE_DIRS"
_COMMAND_NAMES_SAVES = re.compile("(?i)" + "|".join((_SAVES_WORD, _SAVE_NAME, _MY_GAMES, _SAVE_DIRS_VAR)))
_COMMAND_WORDS = re.compile(r""""([^"]*)"|'([^']*)'|((?:\\.|[^\s"';|&()<>\\])+)""")


def _bash_unquoted(command: str) -> str:
    """The text with bash's quoting taken out, so `Sa''ves`, `Sa""ves`, `S\\aves`, `$'Sav\\x65s'` and `My' 'Games` read
    as the words bash passes on (round 8 review). An over-approximation used only to look for save words: a word
    split by quotes everywhere is joined, never split."""
    def ansi_c(m):
        try:
            return m.group(1).encode("latin-1", "backslashreplace").decode("unicode_escape")
        except (UnicodeError, ValueError):
            return m.group(1)
    text = command.replace("\\\n", "")                    # a line continuation joins `Sa\<newline>ves`
    text = re.sub(r"\$'((?:[^'\\]|\\.)*)'", ansi_c, text)
    text = re.sub(r'\$(?=")', "", text)                     # $"..." (locale string) is a quoted word: `Sa$"ves"`
    return re.sub(r"""['"\\]""", "", text)


def command_names_saves(command: str, base: str, roots, env) -> bool:
    """Issue #26 round 7 (user ruling 2026-11-09): a computed write run from a cwd the guard cannot work out
    (`S=...; cd "$S"` in a command the parser will not trust, `cd "$(...)"`) is refused only when the command's own
    text names a save location: a `Saves` component, a .ess/.skse name, `My Games`, SKYRIM_SAVE_DIRS, or a path-shaped
    word that resolves into the settings area (a custom sLocalSavePath folder). Failing closed on every unknown cwd
    refused 227 ordinary scratch-folder scripts in the priced history; a cd computed at run time with no save
    location anywhere in the text is not seen (documented limit)."""
    if _COMMAND_NAMES_SAVES.search(command) or _COMMAND_NAMES_SAVES.search(_bash_unquoted(command)):
        return True
    for m in _COMMAND_WORDS.finditer(command):
        raw = next(g for g in m.groups() if g is not None)
        # Unquoted, bash reads `My\ Games` as one word, while a Windows path keeps its backslashes: try both.
        words = {raw, re.sub(r"\\(.)", r"\1", raw)} if m.group(3) is not None else {raw}
        for word in words:
            if m.group(3) is not None and re.match(r"[A-Za-z_]\w*=", word):
                word = word.split("=", 1)[1]              # an unquoted assignment value: D=C:/x
            if not word or len(word) >= 1024 or not _PATH_SHAPED.match(word):
                continue
            a = targets.resolve_literal(word, base, env)
            if a is not None and targets.zone(a, roots) == "config":
                return True
    return False


def text_write_targets(code: str, *, python_literals: bool = False) -> List[str]:
    if python_literals:
        return _python_write_targets(code)
    out = []
    for rx in _INTERP_WRITE:
        out += [m.group(2) for m in rx.finditer(code)]
    for m in _INTERP_TWO_LITS.finditer(code):
        moved = "rename" in m.group(0)
        out += [_CopyWord(m.group(4), m.group(2))] + ([_CopyWord(m.group(2), moved=True)] if moved else [])
    return out


def _script_reads_stdin(e, command: str) -> bool:
    """`python tool.py <<EOF` hands the body to a named script as data: it is code only with -i, or for `python -`."""
    if not shellparse._PYTHON.fullmatch(e.program) or not e.stdin_body:
        return False
    seen, program = shellparse.python_options(e.argv)      # `python -X utf8 <<EOF` names no script
    return "i" not in seen and "PYTHONINSPECT" not in command and program != "stdin"


def _unplaced_save(word: str) -> bool:
    """A literal save path in the settings area the guard cannot resolve (an 8.3 name, a glob character in a folder).
    A run-time value (`$S/a.ess`) is not one: it says nothing about where it lands (saves.py KNOWN LIMITS)."""
    low = word.replace("\\", "/").lower()
    return low.endswith(SAVE_EXTS) and not re.search(r"[$`%]", word) and bool(re.search(r"/my games/|/saves/", low))


def write_targets(e) -> List[str]:
    """The files this exec writes in place (redirects included)."""
    out = _redirect_targets(e) + list(tool_write_targets(e))
    p = e.program
    if p == "tee":
        out += _plain_operands(e.argv)
    elif p in ("tee-object", "out-file"):
        out += list(ps_param(e, "filepath", 0)) + list(ps_param(e, "literalpath"))
    elif p in ("set-content", "add-content", "clear-content", "new-item"):
        out += list(ps_param(e, "path", 0)) + list(ps_param(e, "literalpath"))
    elif p in ("io.file.write", "io.file.append"):
        out += list(e.argv[:1])
    elif p == "sed":
        out += _sed_files(e.argv)
    elif p == "dd":
        out += [w[3:] for w in e.argv if w.startswith("of=")]
    return [w for w in out if w]


def copy_targets(e) -> List[Tuple[str, Tuple[str, ...]]]:
    """(destination word, source words) for each copy this exec makes."""
    p, argv = e.program, e.argv
    if p in ("io.file.copy", "io.file.move", "io.file.replace"):
        return [(argv[1], (argv[0],))] if len(argv) > 1 else []
    if p in ("copy-item", "move-item"):
        src = tuple(ps_param(e, "path", 0, slot=("literalpath",))) + tuple(ps_param(e, "literalpath"))
        dst = ps_param(e, "destination", 1, before=(("path", "literalpath"),))
        # B-I1 (final review): `Get-Item X | Copy-Item -Destination D` still writes D; its source arrives on the pipe.
        return [(dst[0] if dst else ".", src)] if src or e.stdin_from == "pipe" else []
    if p in CMD_COPY_PROGRAMS and (e.dialect == "cmd" or p == "xcopy"):     # xcopy is a program in every shell
        words = [w for w in argv if not _CMD_SWITCH.fullmatch(w)]
        if not words:
            return []
        return [(words[-1], tuple(words[:-1]))] if len(words) > 1 else [(".", tuple(words))]
    if p == "robocopy":
        pos = robocopy_positionals(argv)        # fix-limits 14: /XF and /XD lists are exclusions, not files
        return [(pos[1].rstrip("/\\") + "/" + f, (f,)) for f in pos[2:]] if len(pos) > 2 else []
    if p in COPY_PROGRAMS:
        target, words, k = None, [], 0
        while k < len(argv):
            w = argv[k]
            names_target, joined = _cp_target_option(w) if p in ("cp", "mv") else (False, None)
            if (w in ("-t", "--target-directory") or (names_target and joined is None)) and k + 1 < len(argv) and p != "rsync":
                target = argv[k + 1]
                k += 2
                continue
            if names_target and joined is not None:
                target = joined
            elif w.startswith("--target-directory="):
                target = w.split("=", 1)[1]
            elif w in (_RSYNC_VALUED if p == "rsync" else _CP_VALUED):
                k += 2
                continue
            elif w == "--":
                words += list(argv[k + 1:])
                break
            elif not (w.startswith("-") and len(w) > 1):
                words.append(w)
            k += 1
        if target is not None:
            return [(target + "/", tuple(words))] if words else []
        return [(words[-1], tuple(words[:-1]))] if len(words) > 1 else []
    return []


# ------------------------------------------------------------------------------------------- where it lands
def _rel(abs_path: str, dirs) -> Optional[List[str]]:
    """The components of abs_path below the longest of `dirs` that holds it, or None."""
    p, best = abs_path.lower().rstrip("/"), None
    for d in dirs:
        dl = d.lower().rstrip("/")
        if (p == dl or p.startswith(dl + "/")) and (best is None or len(dl) > len(best)):
            best = dl
    if best is None:
        return None
    return [c for c in p[len(best):].split("/") if c]


def game_loads(abs_path: str, roots) -> bool:
    """A plugin/archive path where the game (or MO2) loads it: <install>/Data/<file>, or under MO2 mods/overwrite."""
    if targets.zone(abs_path, roots) == "mo2":
        return True
    rel = _rel(abs_path, roots.install_dirs)
    return rel is not None and len(rel) == 2 and rel[0] == "data" and targets.zone(abs_path, roots) == "install"


def in_install_area(abs_path: str, roots) -> bool:
    """The S-WRITE-INSTALL area: the install's Data/ folder, the config folder, or MO2."""
    z = targets.zone(abs_path, roots)
    if z in ("config", "mo2"):
        return True
    if z != "install":
        return False
    rel = _rel(abs_path, roots.install_dirs)
    return bool(rel) and rel[0] == "data"


# S-WRITE-ROOT: the kinds of file the game and its loaders read from the install root -- executables, dlls (the
# OpenVR runtime, ENB/ReShade proxies, the SKSE loader's), .asi, their configs and shader files. Anything else written
# there is the agent's own scratch or the toolkit's (out.txt, a trace, CLAUDE.md, KNOWLEDGEBASE.local.md, .gitignore,
# skyrim-paths.env, which S-SELF-GUARD already notes): four earlier rulings keep those silent (an inspection's
# `> out.txt`, `echo hi > notes.txt` in the root, a captured Codex payload, the guard's own config).
ROOT_GAME_EXT = (".exe", ".dll", ".asi", ".ini", ".toml", ".cfg", ".json", ".fx", ".fxh")


def root_game_file(name: str) -> bool:
    return not name.startswith(".") and name.lower().endswith(ROOT_GAME_EXT)


def in_install_root(abs_path: str, roots) -> bool:
    """S-WRITE-ROOT: a game or loader file directly in the install's root folder (not Data/, not any subfolder)."""
    if targets.zone(abs_path, roots) != "install":
        return False
    rel = _rel(abs_path, roots.install_dirs)
    return rel is not None and len(rel) == 1 and root_game_file(rel[-1])


def is_install_root(abs_path: str, roots) -> bool:
    """The install root folder itself (a copy destination that takes the source's name)."""
    return targets.zone(abs_path, roots) == "install" and _rel(abs_path, roots.install_dirs) == []


def _looks_like_dir(word: str) -> bool:
    if word.endswith(("/", "\\")):
        return True
    base = re.split(r"[/\\]+", word)[-1]
    return base in ("", ".", "..") or "." not in base


def _name(word: str) -> str:
    return re.split(r"[/\\]+", word.rstrip("/\\"))[-1] or word


# ------------------------------------------------------------------------------------------- fallback (R4, opaque)
# A redirect's target is the ONE word right after it (Task 9 item 0: `"$PY" -c "print(1 if a>b else 'x.esp')"` is a
# comparison in interpreter code, not a write onto x.esp; `=>` and `->` are arrows). A quoted target keeps its spaces. Non-overlapping finditer,
# and each alternative stops at its own delimiter, so the scan is linear.
_REDIRECT_TARGET = re.compile(r"""[0-9&*]?(?<![=\-])>[>|]?[ \t]*(?:"([^"\n]*)"|'([^'\n]*)'|([^\s"'<>|;&()]+))""")
# tee writes EVERY operand it is given (`tee a.log X.esp`), so its words after the options are all targets.
_TEE = re.compile(r"\btee\b", re.IGNORECASE)
# The other write words name their target somewhere after them (`Set-Content -Value 1 -Path X.esp`, `sed -i s/a/b/ F`).
_WRITE_WORD = (r"Set-Content|Add-Content|Out-File|Clear-Content|Tee-Object|::(Write|Append)All"
               r"|\bsed\s+-[A-Za-z]*i|\bsed\s+--in-place|\bof=")
_SEGMENT = re.compile(r"[\n;&|]")


def _words(rest: str) -> List[str]:
    return rest.replace('"', " ").replace("'", " ").replace(",", " ").replace(")", " ").split()


def _segment_hits(text: str) -> List[Tuple[str, str]]:
    """(kind, word) for each classified write target in each statement segment. Linear: one finditer for redirects,
    one search each for tee and the other write words, then one pass over the rest of the segment."""
    out = []
    ww = _compile(_WRITE_WORD, True)
    for seg in _SEGMENT.split(text):
        words = []
        if ">" in seg:
            words += [m.group(1) or m.group(2) or m.group(3) or "" for m in _REDIRECT_TARGET.finditer(seg)]
        m = _TEE.search(seg)
        if m:
            words += [w for w in _words(seg[m.end():]) if not w.startswith("-")]
        m = ww.search(seg)
        if m:
            words += _words(seg[m.end():])
        for word in words:
            kind = file_class(word)
            if kind:
                out.append((kind, word))
    return out


# ------------------------------------------------------------------------------------------- the scan
class Scan(NamedTuple):
    deny: str                               # basename of a plugin a write primitive targets ("" for none)
    advice: Tuple[Tuple[str, str], ...]     # (rule id, file phrase) in rule order, one per rule
    move_root: str = ""                     # S-MOVE-ROOT: basename of a protected folder a move takes away
    # The save policy (saves.py): (resolved path, op) for each save in the settings folder a write, copy onto, move
    # or rename CHANGES -- op "write" (written or replaced) or "move" (taken from where it is). Not a decision here:
    # saves.judge decides the call on every save it changes, together.
    save_targets: Tuple[Tuple[str, str], ...] = ()
    sed_crlf: str = ""                      # S-SED-CRLF: basename of a file sed -i rewrites without -b


# ------------------------------------------------------------------------- saves and protected folders (rt-fix-b)
SAVE_EXTS = (".ess", ".skse")
_CONFIG_SAVES = re.compile(r"/documents/my games/skyrim[^/]*/saves$")


def is_save(abs_path: Optional[str], roots) -> bool:
    """A save (or its SKSE co-save) in the settings folder, where the game reads it."""
    return bool(abs_path) and abs_path.lower().endswith(SAVE_EXTS) and targets.zone(abs_path, roots) == "config"


def is_save_folder(abs_path: Optional[str], roots) -> bool:
    """A Saves folder in the settings area: a bulk copy into it can replace any save there."""
    if not abs_path or targets.zone(abs_path, roots) != "config":
        return False
    # Below the settings folder only (sLocalSavePath can name a folder below Saves): a parent folder of the settings
    # folder that happens to be called Saves says nothing.
    below = _rel(abs_path, roots.config_dirs)
    if below is None:
        m = re.search(r"/my games/skyrim[^/]*/(.*)$", abs_path.rstrip("/").lower())
        below = m.group(1).split("/") if m else []
    return "saves" in below


def is_settings_folder(abs_path: Optional[str], roots) -> bool:
    """The settings folder itself (the parent of Saves): a tree restored into it replaces same-named saves."""
    if not abs_path or targets.zone(abs_path, roots) != "config":
        return False
    return _rel(abs_path, roots.config_dirs) == [] or bool(re.search(r"/my games/skyrim[^/]*$", abs_path.rstrip("/").lower()))


def _settings_hold_saves(physical: str) -> bool:
    """Is there a save for a tree restored into this settings folder to replace? With none, nothing is at risk, and a
    wildcard approval could name nothing to approve. A folder that cannot be listed counts as holding some."""
    if not physical:
        return True
    failed = []
    for _folder, _dirs, names in os.walk(physical.rstrip("/\\") + "/Saves", onerror=failed.append):   # sLocalSavePath can
        if any(n.lower().endswith(SAVE_EXTS) for n in names):                                            # name a folder below
            return True
    return any(not isinstance(e, FileNotFoundError) for e in failed)


def _keeps_source_folders(e) -> bool:
    """`rsync -R a/b/c dest/` and `cp --parents a/b/c dest/` write dest/a/b/c, so a name with a folder in it does not
    land where it looks. A plain top-level name has no folder to keep."""
    if e.program == "rsync":
        relative = any(w == "--relative" or re.fullmatch(r"-[A-Za-z]*R[A-Za-z]*", w) for w in e.argv)
    else:
        relative = e.program == "cp" and any(re.fullmatch(r"--pa(?:r(?:e(?:n(?:ts?)?)?)?)?", w) for w in e.argv)
    if not relative:
        return False
    sources = [s for _, words in copy_targets(e) for s in words]
    return any("/" in s.replace("\\", "/") and (_name(s).lower().endswith(SAVE_EXTS) or _may_bring_saves(s)) for s in sources)


def _may_bring_saves(source: str) -> bool:
    """A copy source that can hold saves: a folder, a wildcard that could match one. A named other file cannot."""
    name = _name(source)
    if re.search(r"[*?\[{]", name):
        import fnmatch
        return any(fnmatch.fnmatchcase("x" + ext, name.lower()) or fnmatch.fnmatchcase("save" + ext, name.lower())
                   for ext in SAVE_EXTS) or not re.search(r"\.[a-z0-9]+$", name.lower())
    return _looks_like_dir(source)


def bulk_destinations(e) -> List[str]:
    """Folders this exec fills with files it does not name: an archive extraction, or a tree copy (release review
    2026-11-02: `tar -xf x -C Saves`, `rsync -a bk/ Saves/` and `robocopy bk Saves` changed saves with no save plan)."""
    p, argv = e.program, e.argv
    if p == "tar" and (any(w in ("--extract", "--get") or re.fullmatch(r"-[a-zA-Z]*x[a-zA-Z]*", w) for w in argv)
                       or re.fullmatch(r"[a-zA-Z]*x[a-zA-Z]*", argv[0] if argv else "")):
        return (_opt_values(argv, ("--directory",), ("-C",)) or ["."])[:1]
    if p == "unzip":
        return (_opt_values(argv, (), ("-d",)) or ["."])[:1]
    if p in ("7z", "7za", "7zr", "7z.exe") and argv and argv[0].lower() in ("x", "e"):
        out = [w[2:] for w in argv if w.startswith("-o") and len(w) > 2]
        return out[:1] or ["."]
    if p == "expand-archive":
        return (list(ps_param(e, "destinationpath", 1, before=(("path", "literalpath"),))) or ["."])[:1]
    if p in ("rsync", "scp"):
        return _remote_copy_operands(e)[-1:]
    if p == "robocopy":
        pos = robocopy_positionals(argv)
        return [pos[1]] if len(pos) == 2 else []
    return []


def _save_only_writes(e) -> List[str]:
    """Programs that replace a file by name and are judged elsewhere, read here only for the save policy."""
    p, argv = e.program, e.argv
    if p == "shred":
        return _plain_operands_valued(argv, ("-n", "--iterations", "-s", "--size", "--random-source"))
    if p == "install" and not any(w in ("-d", "--directory") for w in argv):
        return _plain_operands_valued(argv, ("-m", "--mode", "-o", "--owner", "-g", "--group", "-t",
                                             "--target-directory"))[-1:]
    if p == "ln":
        return _plain_operands(argv)[-1:]
    return []


def is_protected_folder(abs_path: Optional[str], roots) -> bool:
    """A folder whose MOVE takes the game's files away: a protected root itself or an ancestor of one
    (targets.covers_protected), the install's Data folder, or the settings folder's Saves folder."""
    if not abs_path:
        return False
    if targets.covers_protected(abs_path, roots):
        return True
    if _rel(abs_path, roots.install_dirs) == ["data"] and targets.zone(abs_path, roots) == "install":
        return True
    return _rel(abs_path, roots.config_dirs) == ["saves"] or bool(_CONFIG_SAVES.search(abs_path.lower()))


MOVE_PROGRAMS = frozenset(("mv", "move-item"))


def move_sources(e) -> List[str]:
    """The words a move or rename takes away from where they are."""
    p, argv = e.program, e.argv
    if p in MOVE_PROGRAMS or (p == "move" and e.dialect == "cmd") or p in ("io.file.move", "io.directory.move", "io.file.replace"):
        return [w for _, srcs in copy_targets(e) for w in srcs] if p not in ("io.file.move", "io.directory.move", "io.file.replace") \
            else list(argv[:1])
    # `rename-item`: since the merge with rt-fix-a (2026-10-05) the parser also emits every Rename-Item that names a
    # -NewName as a `move-item` (shellparse._rename_as_move), judged by the branch above. A Rename-Item without one
    # cannot run (NewName is mandatory), so a branch here only duplicated it.
    if p == "rename":
        words = [w for w in argv if not _CMD_SWITCH.fullmatch(w)]
        return words[:1]
    return []


# Plan 8 T6: the programs whose tool_write_targets are archives they create or update (S-WRITE-ARCHIVE, not the
# redirect note). A redirect on the same exec (`7z a x.7z d > log.txt`) is still S-WRITE-INSTALL.
ARCHIVE_PROGRAMS = frozenset(("compress-archive", "7z", "7za", "7zr", "7z.exe", "zip", "tar"))
ADVICE_ORDER = ("S-COPY-PLUGIN", "S-WRITE-LOADORDER", "S-WRITE-INI", "S-WRITE-SKSE-INI", "S-WRITE-ARCHIVE",
                "S-WRITE-INSTALL", "S-WRITE-ROOT",
                "S-SED-WINPATH")


def _phrase(names: List[str]) -> str:
    uniq = list(dict.fromkeys(names))
    return uniq[0] + (" and %d more" % (len(uniq) - 1) if len(uniq) > 1 else "")


RULE_OF_CLASS = dict((k, r) for k, _, r in CLASS_RULE)
# An UNRESOLVED destination (`"$GAME/Data/My.esp"`) whose own words put a plugin directly in a Data folder: priced,
# 17 real deploys of this shape were silent when an unresolved destination got no note at all.
_DATA_CHILD = re.compile(r"(^|[/\\])data[/\\]+[^/\\]+$", re.IGNORECASE)


def _opaque_texts(execs) -> List[str]:
    """The words an opaque program may run (fix: per program, not per command). Its own argv and stdin body, and --
    when it reads a pipe -- the nearest upstream exec that is not itself a shell: `printf 'echo x > X.esp' | sh`.
    A harness that only PASSES a write as data to a function (`run "printf x > X.esp"`) is not upstream of it."""
    out = []
    for i, e in enumerate(execs):
        if e.program != "?":
            continue
        out.append(" ".join(e.argv) + ("\n" + e.stdin_body if e.stdin_body else ""))
        if e.stdin_from == "pipe":
            for up in reversed(execs[:i]):
                if up.program == "?" or up.program in shellparse.ALL_SHELLS:
                    continue
                out.append(" ".join(up.argv) + ("\n" + up.stdin_body if up.stdin_body else ""))
                break
    return out


def unplaced_mutation(command: str, parsed, env: Mapping[str, str]) -> bool:
    """Known mutations requiring a directory the native host did not report.

    Reuse the policy's operand readers with an unknown initial directory.
    Literal absolute paths still resolve; read operands never enter this set.
    Directory changes may fail and the parser does not prove success paths.
    Opaque programs retain their existing
    coverage limits: this does not claim to enumerate arbitrary script effects.
    """
    from . import anchored, deletes, patch_paths
    from modding_harness.transport import execution_path_absolute
    if not parsed.ok:
        return True
    for original in parsed.execs:
        e = substituted(original)._replace(cd_before="")
        words = list(deletes.operands(e) or ()) + write_targets(e) + move_sources(e)
        words += [dst for dst, _sources in copy_targets(e)]
        for word in words:
            placed = targets.resolve_operand(e, word, "", env) if word is not None else None
            if not execution_path_absolute(placed):
                return True
        if e.program == "git":
            split = anchored._git_split(e.argv)
            if split and anchored._git_discards(split[1], split[2]):
                base = targets.exec_cwd(e, "", env)
                for directory in split[0]:
                    base = targets.resolve(directory, base or "", e.dialect, env)
                if not execution_path_absolute(base):
                    return True
        code = interpreter_code(e)
        if code:
            base = targets.exec_cwd(e, "", env) or ""
            try:
                literal_targets = text_write_targets(code, python_literals=bool(shellparse._PYTHON.fullmatch(e.program)))
            except ValueError:
                # An unreadable Python scope cannot prove that every mutation
                # is absolute. Refuse with the missing-context verdict instead
                # of losing the engine's answer to an internal exception.
                return True
            if any(not execution_path_absolute(targets.resolve_literal(w, base, env)) for w in
                   literal_targets):
                return True
    # .NET static APIs resolve against the process directory, not Set-Location.
    if any(not execution_path_absolute(targets.resolve_literal(w, "", env)) for w in text_write_targets_ps(command)):
        return True
    if any(not execution_path_absolute(targets.resolve_literal(w, "", env)) for w, _how in
           deletes.recycle_targets(deletes.recycle_text(command, parsed))):
        return True
    found = patch_paths.shell_patch(command)
    if found:
        body, directory = found
        if any(not execution_path_absolute(path) for _op, path in patch_paths.parse_patch(body)):
            return True
    return False


def scan(command: str, parsed, roots, cwd: str = "", env: Optional[Mapping[str, str]] = None) -> Scan:
    env = os.environ if env is None else env
    plugin: List[str] = []
    moved: List[str] = []
    saves: List[Tuple[str, str]] = []
    crlf: List[str] = []
    notes = {k: [] for k in ADVICE_ORDER}

    for e in parsed.execs:
        # fix-unclear 4b: a target held in a same-command literal (`P=X.esp; echo x > "$P"`, `$p = ...; Set-Content
        # $p`) is judged as its literal -- the delete rules' own resolver (Exec.subst), argv and redirects alike.
        e = substituted(e)
        sed_files = set(_sed_files(e.argv)) if e.program == "sed" else set()
        winpath = bool(sed_files) and any(sed_win_replacement(s) for s in _sed_scripts(e.argv))
        text_mode = bool(sed_files) and not sed_binary(e.argv)
        archives = set(tool_write_targets(e)) if e.program in ARCHIVE_PROGRAMS else set()
        for w in write_targets(e):
            a = targets.resolve_operand(e, w, cwd, env)
            kind = file_class(w, a or "")
            if kind == "plugin":
                plugin.append(_name(w))
                continue
            # S-SED-WINPATH (Task 9): sed -i on an install/config/MO2 file whose replacement holds a backslash path.
            # It REPLACES the S-WRITE-INSTALL note for that file (one note per write, not two).
            if is_save(a, roots):
                saves.append((targets.physical_operand(e, w, cwd, env) or "", "write"))
                continue
            # S-SED-CRLF (P8-SED): a game file by name, or any file in the install / config / MO2 area.
            in_zone = targets.zone(a, roots) in SED_ZONES if a is not None else False
            if text_mode and w in sed_files and (kind or in_zone):
                crlf.append(_name(w))
            sed_note = winpath and w in sed_files and a is not None and targets.zone(a, roots) in SED_ZONES
            if sed_note:
                notes["S-SED-WINPATH"].append(_name(w))
            if kind:
                notes[RULE_OF_CLASS[kind]].append(_name(w))
            elif not sed_note and a is not None and in_install_area(a, roots):
                notes["S-WRITE-ARCHIVE" if w in archives else "S-WRITE-INSTALL"].append(_name(w))
            elif not sed_note and a is not None and in_install_root(a, roots):
                notes["S-WRITE-ROOT"].append(_name(w))
        for w in move_sources(e):
            a = targets.resolve_operand(e, w, cwd, env)
            if is_protected_folder(a, roots):
                moved.append(posixpath.basename(a.rstrip("/")) or a)
            elif is_save(a, roots):
                saves.append((targets.physical_operand(e, w, cwd, env) or "", "move"))
        saves.extend((targets.physical_operand(e, w, cwd, env) or "", "write") for w in _save_only_writes(e)
                     if is_save(targets.resolve_operand(e, w, cwd, env), roots))

        def bulk_into_saves(word, below=""):
            """A wildcard change of that Saves folder: the typed approval, like any bulk change (saves.judge)."""
            physical = targets.physical_operand(e, word, cwd, env)
            saves.append((physical.rstrip("/\\") + below + "/*" if physical else "", "write"))

        for dst in bulk_destinations(e):
            a_bulk = targets.resolve_operand(e, dst, cwd, env)
            if e.program in ("rsync", "scp") and is_save(a_bulk, roots):
                saves.append((targets.physical_operand(e, dst, cwd, env) or "", "write"))     # one named save, as cp
            elif is_save_folder(a_bulk, roots):
                named = _remote_copy_operands(e)[:-1] if e.program in ("rsync", "scp") else []
                if named and all(s.lower().endswith(SAVE_EXTS) for s in named):
                    physical = targets.physical_operand(e, dst, cwd, env) or ""
                    saves.extend((paths.physical_abs(physical.rstrip("/\\") + "/" + _name(s)) if physical else "", "write")
                                 for s in named)
                else:
                    bulk_into_saves(dst)
            elif is_settings_folder(a_bulk, roots) and (
                    e.program not in ("rsync", "scp") or any(_may_bring_saves(s) for s in _remote_copy_operands(e)[:-1])) and (
                    _settings_hold_saves(targets.physical_operand(e, dst, cwd, env))):
                bulk_into_saves(dst, "/Saves")
        for dst, srcs in copy_targets(e):
            # The destination as written AND where it resolves (`cp a.ini x.ini` run inside Data/SKSE/Plugins).
            a_dst = targets.resolve_operand(e, dst, cwd, env)
            # A save replaced: the destination itself, or a folder destination that takes a save's name.
            if is_save(a_dst, roots):
                saves.append((targets.physical_operand(e, dst, cwd, env) or "", "write"))
                continue
            if is_settings_folder(a_dst, roots) and _keeps_source_folders(e) and _settings_hold_saves(
                    targets.physical_operand(e, dst, cwd, env)):
                bulk_into_saves(dst, "/Saves")
                continue
            if is_save_folder(a_dst, roots) and (not srcs or any(_may_bring_saves(s) for s in srcs
                                                                 if not s.lower().endswith(SAVE_EXTS))):
                bulk_into_saves(dst)
                continue
            if is_settings_folder(a_dst, roots) and any(_may_bring_saves(s) for s in srcs) and _settings_hold_saves(
                    targets.physical_operand(e, dst, cwd, env)):
                bulk_into_saves(dst, "/Saves")
                continue
            if a_dst is None and _unplaced_save(dst):
                saves.append(("", "write"))        # a real path the guard cannot resolve: S-SAVE-UNKNOWN, as saves.py says
                continue
            if a_dst is not None and _looks_like_dir(dst):
                save_dests = [a_dst.rstrip("/") + "/" + _name(s) for s in srcs
                              if s.lower().endswith(SAVE_EXTS)]
                save_dests = [p for p in save_dests if is_save(p, roots)]
                if save_dests:
                    physical_dst = targets.physical_operand(e, dst, cwd, env)
                    saves.extend((paths.physical_abs(physical_dst.rstrip("/\\") + "/" + _name(p))
                                  if physical_dst else "", "write") for p in save_dests)
                    continue
            kind = file_class(dst, a_dst or "")
            if kind == "plugin" and any(s.lower() in NULL_TARGETS for s in srcs):
                plugin.append(_name(dst))                           # cp /dev/null X.esp truncates it: a write
                continue
            if kind and kind != "plugin":
                notes[RULE_OF_CLASS[kind]].append(_name(dst))      # cp x plugins.txt: a write of the load order
                continue
            # S-WRITE-ROOT: a file copied or moved directly into the install root, named or taking its source's name.
            # A destination that names a folder (`Data`, `bindings`) is not a game file (priced: 3 of 8 first-draft
            # hits; ROOT_GAME_EXT excludes it). A bare new name in a move made from Rename-Item lives in the SOURCE's folder, not
            # the cwd (priced: 1 of 8, a .disabled rename inside Data/SKSE/Plugins).
            root_dst = a_dst
            if e.program == "move-item" and srcs and not re.search(r"[/\\]", dst):
                a_src = targets.resolve_operand(e, srcs[0], cwd, env)
                root_dst = posixpath.join(posixpath.dirname(a_src), dst) if a_src else None
            if not kind and root_dst is not None:
                if in_install_root(root_dst, roots):
                    notes["S-WRITE-ROOT"].append(_name(dst))
                    continue
                if is_install_root(root_dst.rstrip("/"), roots):
                    names = [_name(x) for x in srcs if root_game_file(_name(x)) and file_class(x) != "plugin"]
                    notes["S-WRITE-ROOT"].extend(names)
                    if names:
                        continue
            if kind == "plugin":
                cand, name = dst, _name(dst)
            elif _looks_like_dir(dst):
                hit = next((s for s in srcs if file_class(s) == "plugin"), None)
                if hit is None:
                    continue
                cand, name = dst.rstrip("/\\") + "/" + _name(hit), _name(hit)
            else:
                continue
            a = targets.resolve_operand(e, cand, cwd, env)
            if (a is not None and game_loads(a, roots)) or (a is None and _DATA_CHILD.search(cand)):
                notes["S-COPY-PLUGIN"].append(name)

    # routes red team: a file written by name in interpreter code (`python -c "open('X.esp','wb')"`, node
    # writeFileSync) or by a .NET call in PowerShell text (WebClient.DownloadFile, FileStream, [IO.File]::Open).
    # Only literal names. fix-limits 9: each is resolved where the code runs (the exec's cwd, after any cd before it)
    # and judged exactly as a shell write to that place is: a plugin's raw write anywhere, a save, a file class by
    # where it lands (an SKSE INI), any other file in the install area. It was judged by its name alone.
    lit = [(None, w) for w in text_write_targets_ps(command)]
    for e in parsed.execs:
        code = interpreter_code(e)
        if code:
            try:
                lit += [(e, w) for w in text_write_targets(code, python_literals=bool(shellparse._PYTHON.fullmatch(e.program)))]
                if _runs_past_a_syntax_error(e, code):
                    raise ValueError("Python literal write scope could not be parsed")
            except ValueError:
                if not _script_reads_stdin(e, command):
                    raise
            # Issue #26: a computed write target in code that names a save, the Saves folder or the settings folder
            # may change saves the guard cannot list or back up: S-SAVE-UNKNOWN, as for any save it cannot place.
            if shellparse._PYTHON.fullmatch(e.program) and not _script_reads_stdin(e, command):
                found = python_computed_writes(code)
                if found.computed:
                    base = targets.exec_cwd(e, cwd, env) or ""

                    def names_saves(s):
                        a = targets.resolve_literal(s, base, env) if len(s) < 1024 and "\n" not in s else None
                        # A literal inside Saves carries a `Saves` component; run from the settings area is the
                        # cwd check below. Resolving adds a relative settings folder (`Path('Skyrim VR')` from My Games).
                        return is_settings_folder(a, roots) or names_save_location(s)

                    here = targets.resolve_literal(".", base, env) if base else None
                    # #26 round 4 review: run from the settings area, a computed target needs no literal at all
                    # (`cd Saves && python -c "...extractall()"`, `os.listdir()` + rename).
                    # an unknown cwd (`cd "$D" && python ...`) may be the settings area: refused when the command
                    # names a save location anywhere in its text (round 7, user ruling)
                    unknown = here is None and command_names_saves(command, targets.normalise_cwd(cwd) or "", roots, env)
                    if unknown or here is not None and targets.zone(here, roots) == "config" or any(
                            names_saves(s) for s in found.literals):
                        saves.append((COMPUTED_SAVE, "write"))
    for e, word in lit:
        base = cwd if e is None else (targets.exec_cwd(e, cwd, env) or "")
        a = targets.resolve_literal(word, base, env)                    # a literal: nothing in it expands
        kind = file_class(word, a or "")
        copied = isinstance(word, _CopyWord)
        if copied and word.removed and not is_save(a, roots):
            continue
        actual_base = cwd if e is None else (targets.physical_cwd(e, cwd, env) or "")
        if kind == "plugin" and copied:
            if not word.moved and a is not None and game_loads(a, roots):
                notes["S-COPY-PLUGIN"].append(_name(word))       # as `cp`: advice in the load path, silent elsewhere
        elif kind == "plugin":
            plugin.append(_name(word))
        elif is_save(a, roots):
            saves.append((targets.physical(word, actual_base, "cmd", env, literal=True) or "", "write"))
        elif copied and not word.moved and is_save_folder(a, roots) and word.source.lower().endswith(SAVE_EXTS) and not word.tree:
            physical = targets.physical(word, actual_base, "cmd", env, literal=True)
            saves.append((paths.physical_abs(physical.rstrip("/\\") + "/" + _name(word.source)) if physical else "", "write"))
        elif copied and not word.moved and (is_save_folder(a, roots) or (
                word.tree and is_settings_folder(a, roots) and _settings_hold_saves(
                    targets.physical(word, actual_base, "cmd", env, literal=True)))) and (
                word.tree or _may_bring_saves(word.source)):
            physical = targets.physical(word, actual_base, "cmd", env, literal=True)
            below = "/Saves" if is_settings_folder(a, roots) else ""
            saves.append((physical.rstrip("/\\") + below + "/*" if physical else "", "write"))
        elif kind:
            notes[RULE_OF_CLASS[kind]].append(_name(word))
        elif a is not None and in_install_area(a, roots):
            notes["S-WRITE-INSTALL"].append(_name(word))
        elif a is not None and in_install_root(a, roots):
            notes["S-WRITE-ROOT"].append(_name(word))

    texts = [command] if not parsed.ok else _opaque_texts(parsed.execs)
    for t in texts:
        for kind, word in _segment_hits(t):
            if kind == "plugin":
                plugin.append(_name(word))
            else:
                notes[RULE_OF_CLASS[kind]].append(_name(word))
    extra = dict(move_root=_phrase(moved) if moved else "", save_targets=tuple(dict.fromkeys(saves)),
                 sed_crlf=_phrase(crlf) if crlf else "")
    if plugin:
        return Scan(_phrase(plugin), (), **extra)
    return Scan("", tuple((r, _phrase(notes[r])) for r in ADVICE_ORDER if notes[r]), **extra)


