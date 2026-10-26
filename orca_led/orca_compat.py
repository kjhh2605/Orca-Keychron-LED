"""Find Orca's renderer modules in the installed bundle; apply the version policy.

Orca's chunk file names and export aliases are minified per build, so they are
discovered instead of pinned. This reads the installed app.asar (stdlib only),
anchors on the Cmd+number handler (`onJumpToWorktreeIndex`) and resolves the
numbered-order function and workspace store that handler itself uses. Nothing
here runs Orca code. The renderer later imports only modules the entry script
already imported statically (cached instances, no re-execution) and compares
the function's source text before calling it; no other export is ever probed.

orca_compat.json is the single list of live-verified Orca versions. Other
versions may show read-only LED status when discovery and the renderer's
structural checks pass; workspace navigation stays limited to verified ones.
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import posixpath
import re
import struct
import tempfile
from pathlib import Path
from typing import Callable

ORCA_APP = Path("/Applications/Orca.app")
COMPAT_FILE = Path(__file__).with_name("orca_compat.json")
VERSION_RE = re.compile(r"\d{1,6}(?:\.\d{1,6}){1,3}")
MAX_HEADER_BYTES = 64 * 1024 * 1024
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_MODULES = 5000
MAX_HANDLER_CHARS = 20000
MAX_FUNCTION_CHARS = 64 * 1024
MAX_PREAMBLE_CHARS = 1024 * 1024

_NAME = r"[A-Za-z_$][\w$]*"
_CHUNK_SPEC = re.compile(r"\./([A-Za-z0-9_.-]+\.js)")
_IMPORT = re.compile(
    r"import\s*(?:(?P<default>" + _NAME + r")\s*,?\s*)?"
    r"(?:\{(?P<named>[^{}]*)\}|\*\s*as\s+(?P<ns>" + _NAME + r"))?\s*(?:from\s*)?"
    r"(?P<q>[\"'])(?P<spec>[^\"'\n]+)(?P=q)\s*;?")
_EXPORT_TAIL = re.compile(r"export\s*\{(?P<items>[^{}]*)\}\s*;?\s*(?://[^\n]*\s*)*")
_BINDING = re.compile(r"\s*(" + _NAME + r")(?:\s+as\s+(" + _NAME + r"))?\s*")
_HANDLER = re.compile(
    r"\s*(?:\(\s*(?P<a>" + _NAME + r")\s*\)|(?P<b>" + _NAME + r"))\s*=>\s*\{"
    r"|\s*function\s*(?:" + _NAME + r")?\s*\(\s*(?P<c>" + _NAME + r")\s*\)\s*\{")
_VITE_PREAMBLE = re.compile(r"const\s+__vite__mapDeps\s*=")
_ANCHOR = re.compile(r"\.onJumpToWorktreeIndex\(")
_PAIRS = {"(": ")", "[": "]", "{": "}"}


class CompatError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _changed(message: str) -> CompatError:
    return CompatError("orca_layout_changed", message)


def _unavailable(message: str) -> CompatError:
    return CompatError("orca_bundle_unavailable", message)


# --- Compatibility policy -------------------------------------------------

def _version_key(version: str) -> tuple:
    return tuple(int(part) for part in version.split("."))


def _load_policy(path: Path) -> list[str]:
    data = json.loads(path.read_text("utf-8"))
    versions = data.get("verified_versions") if isinstance(data, dict) and data.get("schema") == 1 else None
    if (not isinstance(versions, list) or
            not all(isinstance(v, str) and VERSION_RE.fullmatch(v) for v in versions)):
        raise ValueError("Invalid Orca compatibility file")
    return versions


def verified_versions(path: Path = COMPAT_FILE) -> frozenset[str]:
    # An unreadable policy verifies nothing: navigation is then refused, while
    # read-only status still follows the discovery and contract checks.
    try:
        return frozenset(_load_policy(path))
    except (OSError, ValueError):
        return frozenset()


def is_verified(version: object, path: Path = COMPAT_FILE) -> bool:
    return isinstance(version, str) and version in verified_versions(path)


def mark_verified(version: str, path: Path = COMPAT_FILE) -> bool:
    """Add a live-verified version; returns False if it was already listed."""
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError("Invalid Orca version")
    versions = _load_policy(path)  # never silently replace a corrupt policy
    if version in versions:
        return False
    value = {"schema": 1, "verified_versions": sorted({*versions, version}, key=_version_key)}
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(value, file, indent=2)
            file.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


# --- Installed bundle ------------------------------------------------------

class OrcaBundle:
    """Read-only view of Orca.app's app.asar, checked against the app's own
    integrity records so the analysed bytes are the ones Electron loads."""

    def __init__(self, app: Path = ORCA_APP):
        self.app = Path(app)
        self.asar = self.app / "Contents/Resources/app.asar"
        try:
            with open(self.app / "Contents/Info.plist", "rb") as file:
                info = plistlib.load(file)
            with open(self.asar, "rb") as file:
                first, header_size = struct.unpack("<II", file.read(8))
                if first != 4 or not 8 <= header_size <= MAX_HEADER_BYTES:
                    raise ValueError("Unexpected asar header")
                header = file.read(header_size)
            payload, length = struct.unpack_from("<II", header)
            if len(header) != header_size or payload + 4 > header_size or length > payload - 4:
                raise ValueError("Truncated asar header")
            raw = header[8:8 + length]
            self._root = json.loads(raw)
        except (OSError, ValueError, struct.error, plistlib.InvalidFileException):
            raise _unavailable("Could not read the installed Orca app bundle.") from None
        if not isinstance(info, dict) or not isinstance(self._root, dict):
            raise _unavailable("The installed Orca app bundle is malformed.")
        integrity = info.get("ElectronAsarIntegrity")
        if integrity is not None:
            record = integrity.get("Resources/app.asar") if isinstance(integrity, dict) else None
            if (not isinstance(record, dict) or record.get("algorithm") != "SHA256" or
                    record.get("hash") != hashlib.sha256(raw).hexdigest()):
                raise _unavailable("Orca's app.asar does not match the app's integrity record.")
        self._base = 8 + header_size
        version = info.get("CFBundleShortVersionString")
        try:
            package = json.loads(self.read("package.json"))
        except ValueError:
            package = None
        if (not isinstance(version, str) or not VERSION_RE.fullmatch(version) or
                not isinstance(package, dict) or package.get("version") != version):
            raise _unavailable("The installed Orca app and its bundled package disagree on the version.")
        self.version = version

    def read(self, path: str) -> bytes:
        parts = path.split("/")
        if any(part in {"", ".", ".."} for part in parts):
            raise _changed("Invalid bundle path.")
        node = self._root
        for part in parts:
            files = node.get("files") if isinstance(node, dict) else None
            node = files.get(part) if isinstance(files, dict) else None
            if not isinstance(node, dict):
                raise _changed(f"Orca's bundle has no {path}.")
        size = node.get("size")
        if "files" in node or "link" in node or type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise _changed(f"Orca's bundle entry {path} is not a regular file.")
        try:
            if node.get("unpacked") is True:
                data = (self.asar.parent / (self.asar.name + ".unpacked") / path).read_bytes()
            else:
                offset = node.get("offset")
                if not isinstance(offset, str) or not offset.isdigit():
                    raise _changed(f"Orca's bundle entry {path} has no valid offset.")
                with open(self.asar, "rb") as file:
                    file.seek(self._base + int(offset))
                    data = file.read(size)
        except OSError:
            raise _unavailable(f"Could not read {path} from the installed Orca app.") from None
        integrity = node.get("integrity")
        if len(data) != size or (integrity is not None and (
                not isinstance(integrity, dict) or integrity.get("algorithm") != "SHA256" or
                integrity.get("hash") != hashlib.sha256(data).hexdigest())):
            raise _unavailable(f"Orca's {path} does not match its integrity record.")
        return data


# --- Module analysis ---------------------------------------------------------

def _scan(text: str, start: int, limit: int, stack: list[str]) -> int:
    """Index where `stack` empties at a closing bracket or, if it starts empty,
    of the first top-level `;`. Skips strings, templates and comments.

    Regex literals are not modelled. A misread fails closed: the handler parse
    finds no unique candidate, or the renderer's source comparison rejects it.
    """
    statement, index, end = not stack, start, min(len(text), start + limit)
    while index < end:
        char = text[index]
        if stack and stack[-1] == "`":
            if char == "\\":
                index += 2
                continue
            if char == "`":
                stack.pop()
            elif char == "$" and text.startswith("{", index + 1):
                stack.append("${")
                index += 1
            index += 1
            continue
        if char in "'\"":
            index += 1
            while index < end and text[index] != char:
                if text[index] == "\n":
                    raise _changed("Unterminated string in Orca's renderer code.")
                index += 2 if text[index] == "\\" else 1
        elif char == "`":
            stack.append("`")
        elif text.startswith("//", index):
            index = text.find("\n", index)
            if index < 0:
                break
        elif text.startswith("/*", index):
            index = text.find("*/", index + 2) + 1
            if index <= 0:
                break
        elif char in _PAIRS:
            stack.append(_PAIRS[char])
        elif char == ";" and statement and not stack:
            return index
        elif char in ")]}":
            expected = stack.pop() if stack else None
            if char != ("}" if expected == "${" else expected):
                raise _changed("Unbalanced brackets in Orca's renderer code.")
            if not stack and not statement:
                return index
        index += 1
    raise _changed("Could not delimit a block in Orca's renderer code.")


def _matching(text: str, start: int, limit: int) -> int:
    """Index of the bracket closing text[start]."""
    return _scan(text, start + 1, limit, [_PAIRS[text[start]]])


def _skip_trivia(text: str, index: int) -> int:
    while True:
        while index < len(text) and text[index].isspace():
            index += 1
        if text.startswith("//", index):
            index = text.find("\n", index)
            index = len(text) if index < 0 else index
        elif text.startswith("/*", index):
            close = text.find("*/", index + 2)
            index = len(text) if close < 0 else close + 2
        else:
            return index


def _bindings(items: str) -> list[tuple[str, str]] | None:
    """[(source name, bound name)] for `a as b, c`; None if any item is unusual."""
    result = []
    for item in items.split(","):
        if not item.strip():
            continue
        match = _BINDING.fullmatch(item)
        if match is None:
            return None
        result.append((match.group(1), match.group(2) or match.group(1)))
    return result


class _Module:
    def __init__(self, name: str, text: str):
        self.name, self.text = name, text
        # Bundlers hoist every static import to the top, after Vite's optional
        # preload-dependency table. Anything else ends the prefix and is left
        # out of the closure: a missed import can only make a needed module
        # unresolvable, never misresolve it.
        self.imports: list[str] = []
        self.bindings: dict[str, tuple[str, str] | None] = {}
        index = _skip_trivia(text, 0)
        if _VITE_PREAMBLE.match(text, index):
            index = _skip_trivia(text, _scan(text, index, MAX_PREAMBLE_CHARS, []) + 1)
        while (match := _IMPORT.match(text, index)) is not None:
            chunk = _CHUNK_SPEC.fullmatch(match.group("spec"))
            index = _skip_trivia(text, match.end())
            if chunk is None:
                continue
            target = posixpath.join(posixpath.dirname(name), chunk.group(1))
            self.imports.append(target)
            bound = []
            if match.group("default"):
                bound.append(("default", match.group("default")))
            if match.group("ns"):
                bound.append(("*", match.group("ns")))
            if match.group("named") is not None:
                named = _bindings(match.group("named"))
                bound.extend(named if named is not None else [])
            for imported, local in bound:
                # A doubly bound name is ambiguous; remember it as unusable.
                self.bindings[local] = None if local in self.bindings else (target, imported)
        self.exports: dict[str, str] = {}
        start = text.rfind("export")
        tail = _EXPORT_TAIL.fullmatch(text, start) if start >= 0 else None
        if tail is not None:
            for local, alias in _bindings(tail.group("items")) or []:
                if alias in self.exports:
                    raise _changed(f"Duplicate export {alias} in {name}.")
                self.exports[alias] = local

    def aliases(self, local: str) -> list[str]:
        return [alias for alias, name in self.exports.items() if name == local]

    def declarations(self, local: str) -> list[re.Match]:
        return list(re.finditer(r"(?<![\w$.])function\s*\*?\s*" + re.escape(local) + r"\s*\(", self.text))


class _Renderer:
    def __init__(self, read: Callable[[str], bytes]):
        self.read, self.modules, self.total = read, {}, 0

    def module(self, name: str) -> _Module:
        if name not in self.modules:
            data = self.read(name)
            self.total += len(data)
            if len(data) > MAX_FILE_BYTES or self.total > MAX_TOTAL_BYTES:
                raise _changed("Orca's renderer bundle exceeds the analysis limit.")
            try:
                self.modules[name] = _Module(name, data.decode("utf-8"))
            except UnicodeDecodeError:
                raise _changed(f"{name} is not UTF-8 text.") from None
        return self.modules[name]

    def entry(self) -> str:
        html = self.read("index.html").decode("utf-8", "replace")
        scripts = [attributes for attributes in re.findall(r"<script\b([^>]*)>", html, re.I)
                   if re.search(r"""\btype\s*=\s*["']module["']""", attributes, re.I)]
        sources = [re.search(r"""\bsrc\s*=\s*["']\./(assets/[A-Za-z0-9_.-]+\.js)["']""", attributes)
                   for attributes in scripts]
        if len(sources) != 1 or sources[0] is None:
            raise _changed("Orca's renderer page no longer has exactly one module entry script.")
        return sources[0].group(1)

    def closure(self, entry: str) -> list[str]:
        """Modules the running renderer has necessarily evaluated already."""
        ordered, pending = [], [entry]
        seen = {entry}
        while pending:
            name = pending.pop()
            ordered.append(name)
            if len(ordered) > MAX_MODULES:
                raise _changed("Orca's renderer has too many modules to analyse.")
            for target in self.module(name).imports:
                if target not in seen:
                    seen.add(target)
                    pending.append(target)
        return ordered

    def resolve_function(self, module: _Module, local: str, closure: set) -> tuple[str, str, str]:
        for _ in range(8):
            imported, declared = module.bindings.get(local), module.declarations(local)
            if local in module.bindings and (imported is None or declared):
                raise _changed(f"{local} has more than one binding in {module.name}.")
            if imported is not None:
                chunk, name = imported
                if chunk not in closure or name not in self.module(chunk).exports:
                    raise _changed(f"{module.name} imports {name} from an unexpected module.")
                module = self.module(chunk)
                local = module.exports[name]
                continue
            aliases = module.aliases(local)
            if len(declared) != 1 or len(aliases) != 1:
                raise _changed(f"The numbered-order function {local} is not a unique exported "
                               f"function declaration in {module.name}.")
            start = declared[0].start()
            params = module.text.index("(", declared[0].end() - 1)
            close = _matching(module.text, params, MAX_FUNCTION_CHARS)
            body = _skip_trivia(module.text, close + 1)
            if not module.text.startswith("{", body):
                raise _changed(f"Could not read the numbered-order function in {module.name}.")
            end = _matching(module.text, body, MAX_FUNCTION_CHARS)
            return module.name, aliases[0], module.text[start:end + 1]
        raise _changed("The numbered-order function is re-exported too many times.")

    def resolve_binding(self, module: _Module, local: str, closure: set) -> tuple[str, str]:
        if local in module.bindings:
            imported = module.bindings[local]
            if imported is None:
                raise _changed(f"{local} has more than one binding in {module.name}.")
            chunk, name = imported
            if chunk not in closure or name not in self.module(chunk).exports:
                raise _changed(f"{module.name} imports {name} from an unexpected module.")
            return chunk, name
        aliases = module.aliases(local)
        if len(aliases) != 1:
            raise _changed(f"The workspace store {local} is not uniquely exported by {module.name}.")
        return module.name, aliases[0]


def discover(read: Callable[[str], bytes]) -> dict:
    """Resolve the Cmd+number order function and store from renderer files.

    read(path) returns a file relative to out/renderer, e.g. "index.html".
    """
    renderer = _Renderer(read)
    entry = renderer.entry()
    names = renderer.closure(entry)
    closure = set(names)
    anchors = [(name, match) for name in names for match in _ANCHOR.finditer(renderer.module(name).text)]
    if len(anchors) != 1:
        raise _changed(f"Expected one Cmd+number handler in Orca's renderer; found {len(anchors)}.")
    name, anchor = anchors[0]
    module = renderer.module(name)
    handler = _HANDLER.match(module.text, anchor.end())
    if handler is None:
        raise _changed("Orca's Cmd+number handler has an unexpected form.")
    parameter = handler.group("a") or handler.group("b") or handler.group("c")
    brace = handler.end() - 1
    body = module.text[brace + 1:_matching(module.text, brace, MAX_HANDLER_CHARS)]
    # The handler indexes the numbered order with the shortcut number and reads
    # the store it checks the active view against. Each must be unambiguous.
    orders = set(re.findall(r"(?<![\w$.])(" + _NAME + r")\(\)\[" + re.escape(parameter) + r"\]", body))
    stores = set(re.findall(r"(?<![\w$.])(" + _NAME + r")\.getState\(\)", body))
    if len(orders) != 1 or len(stores) != 1:
        raise _changed("Orca's Cmd+number handler no longer names one order function and one store.")
    order_module, order_export, order_source = renderer.resolve_function(module, orders.pop(), closure)
    store_module, store_export = renderer.resolve_binding(module, stores.pop(), closure)
    return {"entry": entry, "order_module": order_module, "order_export": order_export,
            "order_source": order_source, "store_module": store_module, "store_export": store_export}


_cache: dict[Path, tuple[tuple, dict]] = {}


def discover_installed(app: Path = ORCA_APP) -> dict:
    """Discovery for the installed app, cached until app.asar changes."""
    app = Path(app)
    try:
        stat = os.stat(app / "Contents/Resources/app.asar")
        info = os.stat(app / "Contents/Info.plist")
        key = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, info.st_mtime_ns, info.st_size)
    except OSError:
        raise _unavailable("Orca is not installed at " + str(app)) from None
    cached = _cache.get(app)
    if cached is not None and cached[0] == key:
        return dict(cached[1])
    bundle = OrcaBundle(app)
    layout = {"version": bundle.version, **discover(lambda path: bundle.read("out/renderer/" + path))}
    _cache[app] = (key, layout)
    return dict(layout)
