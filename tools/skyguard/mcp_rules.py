r"""The DevBench guard (Plan 5 Task 11): the MCP route and the curl / CLI route to the SAME two tools.

DevBench drives the RUNNING game. Three ways reach it, and every one lands on a tool call `tool(args)`:
  - MCP         `mcp__devbench-vr__console` with tool_input = the args (protect-mcp, decide());
  - curl        `curl ... http://127.0.0.1:8921/api/tool/<tool> -d '<json args>'` (also `--data*`, `--json`, and
                PowerShell Invoke-RestMethod / Invoke-WebRequest `-Body`);
  - the CLI     `bash tools/devbench-cli.sh exec "<console command>"` and `... tool <name> '<json args>'`.
Since 4.2 the MCP route also judges a few tools by NAME on any server (rules.MCP_WRITE_TOOLS: SkyLink SE/AE 1.5+
writes plugins, compiled scripts and the load order; rules.MCP_CONSOLE_TOOLS: its console). Their curl/CLI routes
are not read: SkyLink is reached over MCP stdio only.
judge() decides a tool call once, so the three routes cannot disagree; scan_shell() finds the calls in a parsed shell
command (protect-bash) and decide() reads the MCP payload (protect-mcp).

S-DEVBENCH-CRASH (deny). The `console` tool is given a command on the deny list, DEVBENCH_DENY (rules.py -- data, not
code). Today that is `smp reset` with NO argument, the only command MEASURED to crash this install (R14, P11): a full
reset of a 700+ plugin order. Compared on whitespace-split lower-case words, so `SMP  RESET` is the same command;
`smp reset <anything>` is a different (unmeasured) command and passes. A console line may hold several commands
(`;` or a newline): each is judged.

DEVBENCH SAVES: not judged since 4.1 (user ruling 2026-11-10). A DevBench save over an existing save was backed up
first, and refused when the save could not be told; now tools/save_vault.py keeps every version of every save, and
these rules read no save name and no folder.

KNOWN LIMITS (recorded in the pricing ledger, not handled):
  - `curl ... -d @body.json`, `--data-binary @-` and a body held in a variable that is not a same-command literal:
    the body is unreadable offline, so the call is allowed;
  - interpreter code (python requests, node fetch) that posts to the console is not read. A first version denied code
    that held both the endpoint and `smp reset`; priced over history it hit 3 items, all scripts written to TEST this
    rule (strings, not calls), and none a real call, so it was dropped (approval fatigue outweighs an unseen shape);
  - the `papyrus` tool: every string in its arguments is read for a deny-listed console line; a function that runs
    a console line it builds itself is not, and console `bat` files can run one without it being named here;
  - `replay` (and any tool that plays back a stored recording or input script): the recording's contents are not in
    the call, so the console lines it replays are not judged; a deny-listed line is caught only when it is
    written into the call itself (a scenario step is, since its steps travel in the call);
  - a URL held in a variable that is not a same-command literal.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import urllib.parse
from typing import Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from . import deletes, paths, rules
from .verdict import Verdict

HOOK = "protect-mcp"

# The endpoint any HTTP route uses: `/api/tool/<tool>`, host and port irrelevant (the port moves when 8921 is taken).
_ENDPOINT = re.compile(r"/api/tool/([A-Za-z_]+)/?(?:[?#].*)?$")
_MCP_ENDPOINT = re.compile(r"^https?://[^/\s]+/mcp/?(?:[?#].*)?$", re.IGNORECASE)
_CURL_BODY_FLAGS = ("--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode", "--json")
_WEB_PROGRAMS = frozenset(("curl", "wget", "invoke-restmethod", "irm", "invoke-webrequest", "iwr"))
_PS_PROGRAMS = frozenset(("invoke-restmethod", "irm", "invoke-webrequest", "iwr"))
_SPLIT_STATEMENTS = re.compile(r"[;\n\r]")
# The shell-route cheap prefilter: every form names one of these, or both "api" and "tool" (final review B-M4: a dot
# segment or an empty one splits `api/tool`, `/api/./tool/console`).
_SHELL_WORDS = ("api/tool", "devbench", "/mcp")
# A PowerShell .NET web call (shellparse does not model method calls): its body is text only.
_DOTNET_WEB = re.compile(r"\.(?:Upload(?:String|Data|Values)(?:Async)?|(?:Post|Send|Put)Async|GetResponse)\s*\(",
                         re.IGNORECASE)
_TEXT_RESET = re.compile(r"\bsmp\s+reset\b", re.IGNORECASE)
_TEXT_ENDPOINT = re.compile(r"/api/tool/console", re.IGNORECASE)
_KEY_TEXT = r'"%s"\s*:\s*"((?:[^"\\]|\\.)*)"'
# `smp reset` with nothing after it, as it reads in a body whose structure the guard cannot parse: it is followed by a
# quote (plain, escaped or doubled), a bracket, a separator or the end.
_TEXT_BARE_RESET = re.compile(r"""\bsmp\s+reset\s*(?:["'`)}\\\];,&|<>]|$)""", re.IGNORECASE)

Hit = Tuple[str, dict]            # (rule id, message fields)


# routes red team (rt-fix-b): what the console actually reads. A NUL ends the C string the game parses (`smp reset\0x`
# runs `smp reset`); zero-width and other format characters print as nothing; a statement wrapped whole in quotes is
# the same statement.
_INVISIBLE = re.compile("[\x01-\x08\x0b\x0c\x0e-\x1f\x7f­​-‏⁠-⁤﻿]")


def console_text(command: str) -> str:
    return _INVISIBLE.sub("", command.split("\x00", 1)[0])


def _statements(command: str):
    for stmt in _SPLIT_STATEMENTS.split(console_text(command)):
        s = stmt.strip()
        if len(s) > 1 and s[0] == s[-1] and s[0] in "\"'`":
            s = s[1:-1]
        yield s


def deny_op(command: str) -> str:
    """The deny-listed command this console line runs, in its canonical spelling, else ""."""
    for stmt in _statements(command):
        words = stmt.lower().split()
        for denied in rules.DEVBENCH_DENY:
            if tuple(words) == denied:
                return " ".join(denied)
    return ""


def _strings(obj, depth: int = 0):
    """Every string inside a tool's arguments (dict values, list items), depth-capped: a deny-listed console line
    anywhere in a call (a scenario step, a papyrus ExecuteCommand argument, a key the guard does not know) is the
    same command."""
    if depth > 32:
        return
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v, depth + 1)


def _steps(args, depth: int = 0):
    """(tool, args) for each step of a scenario: `{tool|name, args|arguments}` and the `{console: "<line>"}` form."""
    if depth > 8 or not isinstance(args, dict):
        return
    for step in args.get("steps") if isinstance(args.get("steps"), list) else ():
        if not isinstance(step, dict):
            continue
        tool = step.get("tool") if isinstance(step.get("tool"), str) else step.get("name")
        sargs = step.get("args") if isinstance(step.get("args"), dict) else step.get("arguments")
        if isinstance(tool, str) and isinstance(sargs, dict):
            yield tool, sargs
            yield from _steps(sargs, depth + 1)
        if isinstance(step.get("console"), str):
            yield "console", {"command": step["console"]}
        if isinstance(step.get("command"), str):
            yield "console", {"command": step["command"]}
        if isinstance(step.get("action"), str):
            yield "game", step


def _documents(env) -> list:
    """Where `Documents` may be: under USERPROFILE / HOME, and a OneDrive-redirected Documents (OneDrive,
    OneDriveConsumer, OneDriveCommercial). Environment only: nothing here asks the OS."""
    out = []
    for key in ("USERPROFILE", "HOME", "OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        base = env.get(key, "")
        if base:                                              # an empty value would make `Documents` cwd-relative
            d = os.path.join(base, "Documents")
            if d not in out:
                out.append(d)
    return out


def saves_folder(roots, env) -> Optional[str]:
    """The first existing Saves folder: each declared config dir, then <Documents>/My Games/<game>/ for every game
    folder name (VR, SE, base). None when there is none."""
    cands = [os.path.join(d, "Saves") for d in roots.physical_config_dirs or roots.config_dirs]
    for docs in _documents(env):
        cands += [os.path.join(docs, "My Games", g, "Saves") for g in paths.GAME_FOLDERS]
    return next((c for c in cands if os.path.isdir(c)), None)




def judge(tool: str, args, roots, memo: Optional[dict] = None, env=None, depth: int = 0) -> Optional[Hit]:
    """The decision for one DevBench tool call, as (rule id, fields), or None for allow. Any string anywhere in the
    call that the console would run as a deny-listed command denies (rt-fix-b: a list or an unknown key, a scenario
    step, a papyrus ExecuteCommand argument). `roots`, `memo` and `env` are unused since 4.1 (no save is read)."""
    for s in _strings(args):
        op = deny_op(s)
        if op:
            return ("S-DEVBENCH-CRASH", {"op": op})
    if not isinstance(args, dict):
        return None
    if depth < 8:
        for stool, sargs in _steps(args):
            hit = judge(stool, sargs, roots, memo, env, depth + 1)
            if hit:
                return hit
    return None


# ------------------------------------------------------------------------------------------------------ MCP route
def is_devbench_server(server: str) -> bool:
    """A DevBench MCP server by any spelling of its configured name: `devbench-vr`, `DevBench-vr`, `dev-bench`,
    `skyrim-devbench`, a plugin-installed `plugin_devbench_x` (rt-fix-b, routes s106-s111). Case- and
    separator-insensitive."""
    return "devbench" in re.sub(r"[-_.\s]", "", server.lower())


def is_devbench(name: str) -> bool:
    """True for an `mcp__<server>__<tool>` call whose name after `mcp__` contains devbench in any case or spelling
    (is_devbench_server). Plan 5 Task 17: the Codex engine uses it to keep a judged DevBench call out of the
    unhooked-tools log. Plan 8 T6 fix round 1 (review M4): Claude's matcher fires on any tool name containing devbench,
    and a server name may itself hold `__` (`mcp__my__devbench__console`), so the whole remainder is read -- reading
    only the first piece as the server let such a call through unjudged."""
    parts = name.split("__") if isinstance(name, str) else []
    return len(parts) >= 3 and parts[0] == "mcp" and is_devbench_server("__".join(parts[1:]))


def tool_key(tool: str) -> str:
    """A tool name with case and `-_.` separators dropped: SkyLink 1.5.0 sets no explicit name, so `install_plugin`,
    `InstallPlugin` and `installPlugin` are all the same tool."""
    return re.sub(r"[-_.\s]", "", tool.lower())


_NAMED = {tool_key(t): t for t in rules.MCP_WRITE_TOOLS | rules.MCP_CONSOLE_TOOLS}


def named_tool(name: str) -> str:
    """The canonical name (as listed in rules.MCP_WRITE_TOOLS / MCP_CONSOLE_TOOLS) of an `mcp__<server>__<tool>` call
    the rules judge on ANY server (SkyLink SE/AE 1.5+ under whatever server name the user chose), else "". The whole
    last segment must be the tool: `mcp__housecarl__housecarl_compile_script` is not `compile_script`."""
    parts = name.split("__") if isinstance(name, str) else []
    if len(parts) < 3 or parts[0] != "mcp":
        return ""
    return _NAMED.get(tool_key(parts[-1]), "")


# S-MCP-MESH-WRITE (deny, 4.3): Outfit Studio's MCP server (BodySlide and Outfit Studio's wiki, "MCP Server for AI
# agents") has no folder limits ("an agent can do what you can do") and 100+ tools whose names the wiki does not list.
# Not measured: no installed copy to ask `tools/list`. So the rule is by PATH, not by name: on a server whose name says
# Outfit Studio or BodySlide, every tool is judged except one whose name starts with a read verb (load, open, get,
# render ...) and says nothing of saving, exporting, building, writing or steps. A judged call is refused when a path
# in its arguments is an absolute path into the protected area (anchored.mesh_output_protected), or a relative path
# whose `..` climbs out of BodySlide's folder (relative paths resolve against it, and it is inside Data). Paths given
# as INPUTS are not judged: a value under an input-like key (`source`, `inputPath`, `reference` ...), the path keys of
# a listed step whose type is exactly a read (`Load: Outfit`, `Import`), and the path attributes of an automation
# `<Step type="Load: ...">` element -- except on a tool that takes its source away (move, rename, delete ...).
_MESH_WRITE_WORDS = ("save", "export", "build", "write", "step", "automation", "convert", "copy", "output", "move",
                     "rename", "remove", "delete")
# A tool that takes its source AWAY (move, rename, cut, remove, delete): its input paths are judged too.
_MESH_MOVE_WORDS = ("move", "rename", "cut", "remove", "delete")
_MESH_READ_VERBS = frozenset(("load", "open", "read", "import", "validate", "check", "verify", "analyze", "analyse",
                              "compare", "diff", "measure", "count", "scan", "browse", "get", "list", "info",
                              "render", "preview", "inspect",
                              "describe", "status", "help", "search", "find", "view", "show", "query", "select"))


def mesh_tool_judged(tool: str) -> bool:
    """False only for a tool whose name starts with a read verb and holds no write word."""
    if any(w in tool_key(tool) for w in _MESH_WRITE_WORDS):
        return True
    words = [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", tool)]
    return not (words and words[0] in _MESH_READ_VERBS)


def is_outfit_studio(name: str) -> bool:
    parts = name.split("__") if isinstance(name, str) else []
    if len(parts) < 3 or parts[0] != "mcp":
        return False
    server = tool_key("__".join(parts[1:-1]))
    return "outfitstudio" in server or "bodyslide" in server


# Where an absolute path STARTS inside a string (an inline automation script, a JSON-encoded step list, `path=...`):
# a drive path (anywhere not glued to a letter or digit, or after a short flag: `-oC:/...`); a UNC, `//`, POSIX or
# MSYS path (`/home/...`, `/c/...`) only at the start or after a space, quote, `=:,;<>|&+`, a backtick or an opening
# bracket -- so the `/` inside a relative path or another path (`Program Files (x86)/Steam`) never starts one.
# Each may be a file: URI.
_PATH_START = re.compile(r"(?:(?<=-[A-Za-z])|(?<=-[A-Za-z]{2})|(?<![A-Za-z0-9]))(?:file:/*)?[A-Za-z]:/"
                         r"|(?<![^\s\"'=:,;<>\[{(|`&+])(?:file:/*)?(?://|/(?=[\w.~-]))")
_PATH_END = re.compile(r"[\"'<>\r\n|*?]")
# Where a RELATIVE path resolves: Outfit Studio takes it against BodySlide's own folder (its wiki), which a stock
# install keeps inside Data. A `..` that stays inside that folder is fine; one that climbs out reaches Data or beyond.
_BS_HOME = "/_install_/data/calientetools/bodyslide"
_INPUT_KEY = re.compile(r"^(?:input|inputs|source|sources|src|from|reference|ref|load|open|read)"
                        r"(?:path|paths|file|files|dir|folder|nif|name)?$")
_READ_TYPE = re.compile(r"^\s*(?:load|read|import|open)(?:\s*:[^;\n]*)?\s*$", re.IGNORECASE)
_STEP_PATH_KEYS = frozenset(("path", "paths", "file", "files", "filename", "filepath", "nif", "source", "src",
                             "input", "inputpath", "reference", "ref"))
_READ_ELEMENT = re.compile(r"<[^<>]*\btype\s*=\s*[\"']\s*(?:load|read|import|open)(?:\s*:[^\"'<>]*)?[\"'][^<>]*>",
                           re.IGNORECASE)
_STEP_TYPE_KEYS = ("type", "action", "op", "kind", "step", "command")
_ELEMENT_PATH_ATTR = re.compile(r"\b(?:path|file|filename|source|src|input)\s*=\s*(?:\"[^\"]*\"|'[^']*')",
                                re.IGNORECASE)


def _absolute_candidates(text: str):
    """Each absolute path in `text`, one per start (a path list on one line gives each its own candidate). A segment
    runs to the next start or a quote/angle bracket/line end; paths may hold spaces, so the segment cut at each of
    its first 64 spaces is a candidate too when nothing after that space is a path (`.../Data then quit` is judged
    as `.../Data`; `.../Data Exports/x.nif` is not cut to `.../Data`)."""
    t = text.replace("\\", "/")
    starts = [m.start() for m in _PATH_START.finditer(t)]
    for i, s in enumerate(starts):
        stop = starts[i + 1] if i + 1 < len(starts) else len(t)
        end = _PATH_END.search(t, s, stop)
        seg = t[s:end.start() if end else stop]
        cuts = [m.start() for m in re.finditer(r"\s", seg[:4096]) if "/" not in seg[m.start():]][:64]
        for piece in [seg] + [seg[:c] for c in cuts]:
            c = urllib.parse.unquote(re.sub(r"^file:/*", "", piece)) if piece.lower().startswith("file:") else piece
            if re.match(r"^[A-Za-z]:/", c) is None and not c.startswith("/"):
                c = "/" + c.lstrip("/")
            c = c.rstrip(" .,;:`")
            for close, open_ in ((")", "("), ("]", "["), ("}", "{")):   # `(D:/x/Data)`: an unmatched closer is not the path's
                while c.endswith(close) and c.count(close) > c.count(open_):
                    c = c[:-1].rstrip(" .,;:`")
            if len(c) > 1:
                yield c


def _relative_escapes(text: str) -> bool:
    """A relative path (the whole string) whose `..` climbs out of BodySlide's own folder."""
    t = text.replace("\\", "/").strip().strip("\"'")
    if not t or re.match(r"(?:file:|[A-Za-z]:/|/)", t) or ".." not in t.split("/"):
        return False
    resolved = posixpath.normpath(posixpath.join(_BS_HOME, t))
    return not (resolved == _BS_HOME or resolved.startswith(_BS_HOME + "/"))


def _relative_in_text(text: str) -> bool:
    """The whole string, or any token of it (split on spaces, quotes, `=`, `,`, `;`, `|`, angle brackets), is a
    relative path that climbs out of BodySlide's folder: `<Step path="../../meshes/x.nif"/>` inside a script."""
    if _relative_escapes(text):
        return True
    return any(_relative_escapes(tok) for tok in re.split(r"[\s\"'<>=,;|]+", text.replace("\\", "/")) if ".." in tok)


def _mesh_texts(obj, skip_inputs: bool = True, depth: int = 0, in_list: bool = False):
    """The strings an Outfit Studio call could write to: every string in its arguments except INPUTS -- a value under
    an input-like key, the path keys of a step (a LIST item) whose type is exactly a read (`Load: Outfit`, `Import`),
    the path attributes of a `<Step type="Load: ...">` element. A tool that takes its source away (`skip_inputs`
    False: move, rename, delete ...) has its inputs judged too. A string holding JSON is walked the same way."""
    if depth > 32:
        return
    if isinstance(obj, str):
        text = obj.strip()
        if text[:1] in "[{":
            try:
                parsed = json.loads(text)
            except ValueError:
                parsed = None
            else:
                yield from _mesh_texts(parsed, skip_inputs, depth + 1, in_list)
                return
        yield _READ_ELEMENT.sub(lambda m: _ELEMENT_PATH_ATTR.sub(" ", m.group(0)), obj) if skip_inputs else obj
    elif hasattr(obj, "items"):                       # a mapping (spelled apart from _strings: unique twin anchors)
        read_step = in_list and skip_inputs and any(
            isinstance(obj.get(k), str) and _READ_TYPE.match(obj[k]) for k in _STEP_TYPE_KEYS)
        for k, v in obj.items():
            key = tool_key(k) if isinstance(k, str) else ""
            if skip_inputs and (_INPUT_KEY.match(key) or (read_step and key in _STEP_PATH_KEYS)):
                continue
            yield from _mesh_texts(v, skip_inputs, depth + 1, False)
    elif type(obj) in (list, tuple):
        for v in obj:
            yield from _mesh_texts(v, skip_inputs, depth + 1, True)


def mesh_write_target(tool: str, args, roots) -> str:
    """The path argument an Outfit Studio call would write into the protected area ("" for none). A relative `..`
    that leaves BodySlide's folder is refused even without roots; absolute paths need them to be classified. The whole
    of every string is scanned (no length cut)."""
    from . import anchored
    if not mesh_tool_judged(tool):
        return ""
    skip_inputs = not any(w in tool_key(tool) for w in _MESH_MOVE_WORDS)
    for text in _mesh_texts(args, skip_inputs):
        if _relative_in_text(text):
            return text if len(text) <= 300 else text[:300]
        if roots is None:
            continue
        for c in _absolute_candidates(text):
            resolved = paths.norm_abs(c)
            if resolved and anchored.mesh_output_protected(resolved, roots):
                return text if len(text) <= 300 else c
    return ""


def judged_server(name: str) -> bool:
    """True when `name` is an `mcp__<server>__<tool>` call the MCP rules judge: a DevBench server (Plan 5's
    is_devbench), or a named write/console tool on any server (named_tool, 4.2). Plan 7: the neutral entry labels
    every other MCP call NOT JUDGED rather than evaluated."""
    return is_devbench(name) or bool(named_tool(name)) or is_outfit_studio(name)


def decide(name: str, args, audit, roots=None, env=None) -> Verdict:
    """protect-mcp: the verdict for one `mcp__<server>__<tool>` call. Judged: DevBench servers, and the named
    write/console tools of any server (rules.MCP_WRITE_TOOLS deny; rules.MCP_CONSOLE_TOOLS read like DevBench's
    console). Every other MCP call is allowed here."""
    if name == "" or name == "unknown":
        audit.rule(HOOK, "S-INERT-NOINPUT", "deny", "no tool call in payload -- the DevBench guard checked nothing",
                   level="REFUSE")
        return Verdict("deny", rules.message("S-INERT-NOINPUT"))
    parts = name.split("__")
    tool = named_tool(name)
    if tool in rules.MCP_WRITE_TOOLS:
        hit = ("S-MCP-WRITE", {"op": tool})
    elif is_devbench(name) or tool in rules.MCP_CONSOLE_TOOLS:
        hit = judge(parts[-1], args, roots, None, env)
    elif is_outfit_studio(name):
        where = mesh_write_target(parts[-1], args, roots)
        hit = ("S-MCP-MESH-WRITE", {"file": where}) if where else None
    else:
        return Verdict()
    if hit is not None:
        rid, fields = hit
        reason = rules.message(rid, **fields)
        audit.rule(HOOK, rid, rules.RULES[rid]["decision"], reason)
        return Verdict(rules.RULES[rid]["decision"], reason)
    return Verdict()        # a DevBench save over an existing save: no rule decides on it (4.1, the save vault)


# ----------------------------------------------------------------------------------------------- curl / CLI route
def _args_from_body(body: str) -> Optional[dict]:
    """The tool's args from a request body: JSON, else the few keys that matter pulled out of damaged JSON."""
    try:
        d = json.loads(body)
        return d if isinstance(d, dict) else None
    except ValueError:
        pass
    out = {}
    for key in ("action", "command", "name"):
        m = re.search(_KEY_TEXT % key, body)
        if m:
            try:
                out[key] = json.loads('"' + m.group(1) + '"')
            except ValueError:
                out[key] = m.group(1)
    return out or None


def _curl_bodies(argv) -> list:
    out, k = [], 0
    while k < len(argv):
        w = argv[k]
        if w in _CURL_BODY_FLAGS or re.fullmatch(r"-[A-Za-z]*d", w):
            if k + 1 < len(argv):
                out.append(argv[k + 1])
            k += 2
            continue
        attached = re.fullmatch(r"-[A-Za-z]*d(.+)", w, re.S)    # curl takes the rest of a cluster as d's value: -sd'{..}'
        if attached and not w.startswith("--"):
            out.append(attached.group(1))
        k += 1
    return out


def _norm_url(word: str) -> str:
    """The word as the server sees the URL (final review B-M4): percent-decoded (rt-fix-b: /api/tool/%63onsole), with
    the path's dot segments and empty segments squashed, as curl squashes them before sending (`/api/./tool/console`,
    `/api/x/../tool/console`, `/api/%2e/tool/console`, `/api//tool/console`). Linear: posixpath.normpath."""
    u = unquote(word)
    if "/" not in u:
        return u
    try:
        parts = urlsplit(u)
    except ValueError:
        return u
    if not parts.path:
        return u
    path = posixpath.normpath(parts.path)
    if path.startswith("//"):
        path = path[1:]                                    # normpath keeps a POSIX leading `//`
    if parts.path.endswith("/") and not path.endswith("/"):
        path += "/"
    return urlunsplit(parts._replace(path=path))


def _norm_text(text: str) -> str:
    """Every URL-like token of the text normalised by _norm_url (the text fallback's endpoint test)."""
    return " ".join(_norm_url(t) for t in re.split(r"[\s'\"`]+", text) if t)


def _web_call(e, text: str) -> list:
    """(tool, args) pairs for each DevBench REST call this curl / Invoke-RestMethod exec makes; unreadable bodies
    (`@file`, a variable) yield nothing."""
    tool, query, rpc = "", {}, False
    for w in e.argv:
        u = _norm_url(w)                                      # rt-fix-b %63onsole; B-M4 dot / empty segments
        m = _ENDPOINT.search(u)
        if m:
            tool = m.group(1)
            q = urlsplit(u).query
            query = {k: v[-1] for k, v in parse_qs(q).items()} if q else {}
            break
        if _MCP_ENDPOINT.search(u):
            tool, rpc = "mcp", True                           # DevBench's MCP-over-HTTP: JSON-RPC tools/call
            break
    if not tool:
        return []
    if e.program in _PS_PROGRAMS:
        from .shellparse import ps_param
        bodies = list(ps_param(e, "body"))
    elif e.program == "wget":
        bodies = [w.split("=", 1)[1] for w in e.argv if w.startswith(("--post-data=", "--body-data="))]
        bodies += [e.argv[i + 1] for i, w in enumerate(e.argv[:-1]) if w in ("--post-data", "--body-data")]
    else:
        bodies = _curl_bodies(e.argv)
    out = [(tool, query)] if query else []
    for b in bodies:
        args = _args_from_body(b)                             # `@file` is no JSON, so a body file yields None
        if rpc and isinstance(args, dict):
            params = args.get("params") if isinstance(args.get("params"), dict) else {}
            name = params.get("name") if isinstance(params.get("name"), str) else "mcp"
            inner = params.get("arguments")
            out.append((name, inner if isinstance(inner, dict) else args))
            continue
        if args is not None and (tool.lower() != "console" or isinstance(args.get("command"), str)):
            out.append((tool, args))
        elif tool.lower() == "console" and _unreadable_names_reset(text):
            out.append(("console", {"command": "smp reset"}))
    return out


def _unreadable_names_reset(text) -> bool:
    """A console call whose body gave no definite command (`-d command=smp reset`, `-Body (@{...} | ConvertTo-Json)`,
    `-Body $b`, a PowerShell-escaped `\\"` body) but whose command text (the WHOLE command: the body is often built in an earlier statement) names `smp reset` bare: the same as the opaque
    branch, it denies. A body file or a variable that does not name it stays allowed (the ledgered gap)."""
    return bool(_TEXT_BARE_RESET.search(text))


def _cli_call(e, text: str) -> list:
    argv = e.argv
    if not argv:
        return []
    if argv[0] == "exec" and len(argv) > 1:
        return [("console", {"action": "exec", "command": " ".join(argv[1:])})]
    if argv[0] == "call" and len(argv) > 2:                  # call <Script> <Function> [argsJson]: the papyrus tool
        try:
            a = json.loads(argv[3]) if len(argv) > 3 else []
        except ValueError:
            a = [argv[3]]
        return [("papyrus", {"action": "call", "script": argv[1], "function": argv[2], "args": a})]
    if argv[0] == "tool" and len(argv) > 2:
        args = _args_from_body(argv[2])
        if args is not None and (argv[1].lower() != "console" or isinstance(args.get("command"), str)):
            return [(argv[1], args)]
        if argv[1].lower() == "console" and _unreadable_names_reset(text):
            return [("console", {"command": "smp reset"})]
        return []
    return []


def scan_shell(command: str, parsed, roots, env=None) -> Optional[Hit]:
    """The first deny among the DevBench calls a shell command makes. Not safe by default
    (R4): where the parse failed or a program is opaque, a deny-listed command next to the console endpoint in the
    TEXT denies. Interpreter code is NOT read (see KNOWN LIMITS)."""
    low = unquote(command).lower()
    if not any(w in low for w in _SHELL_WORDS) and not ("api" in low and "tool" in low):
        return None
    opaque = False
    memo: dict = {}
    for e in parsed.execs:
        if e.program == "?":
            opaque = True
            continue
        e = deletes.substituted(e)
        calls = []
        if e.program in _WEB_PROGRAMS:
            calls = _web_call(e, command)
        elif e.program == "devbench-cli":
            calls = _cli_call(e, command)
        for tool, args in calls:
            hit = judge(tool, args, roots, memo, env)
            if hit is not None:             # judge() returns only the crash refusal since 4.1 (no save ask)
                return hit
    textual = not parsed.ok or opaque or bool(_DOTNET_WEB.search(command))
    if textual and _TEXT_RESET.search(command) and _TEXT_ENDPOINT.search(_norm_text(command)):
        return ("S-DEVBENCH-CRASH", {"op": "smp reset"})
    return None
