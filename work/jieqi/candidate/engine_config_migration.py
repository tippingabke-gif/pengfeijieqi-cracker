"""Install the bundled ACE bridge while preserving the user's XML bytes.

Existing ACE entries keep every setting except their FilePath. Older configs
receive the minimal engine and engine-setting entries used by the application.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid
from xml.parsers import expat

ENGINE_NAME = "ACE_JieQi_64.exe"
ENGINE_SHA256 = "065497ebe421e2337a2c5e83a948964c78e5e99b7058789843ac39d5d77edaea"
BRIDGE_NAME = "PengfeiJieQi-EngineBridge.exe"
BRIDGE_CONFIG_PATH = "Engine/PengfeiJieQi-EngineBridge.exe"
ACE_GUID = "f75c5a9a-767c-4238-a1e0-ba9f3b60447c"
ACE_SETTING_GUID = "86541886-1acc-41c5-a1b3-558a26c08dbd"


class MigrationError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _decode(raw: bytes) -> tuple[str, str]:
    encoding = ("utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else
                "utf-16-le" if raw.startswith(b"\xff\xfe") else
                "utf-16-be" if raw.startswith(b"\xfe\xff") else "utf-8")
    try:
        return raw.decode(encoding), encoding
    except UnicodeError as exc:
        raise MigrationError("E_ENGINE_CONFIG", f"unsupported XML encoding: {exc}") from exc


@dataclass
class _Node:
    tag: str
    start: int
    open_end: int
    empty: bool
    close_start: int = 0
    end: int = 0
    text: str = ""
    children: list["_Node"] = field(default_factory=list)

    @property
    def name(self):
        return self.tag.rsplit(":", 1)[-1]

    def named(self, name):
        return [child for child in self.children if child.name == name]


def _parse(text: str) -> _Node:
    """Parse real XML nodes and retain character spans for surgical edits.

    Expat reports UTF-8 byte offsets even for a Python unicode input. Convert
    those offsets back into text indices; original UTF-16/BOM/CRLF bytes are
    restored at the final encoding step. Comments and CDATA cannot masquerade
    as element locations.
    """
    parser = expat.ParserCreate()
    utf8 = text.encode("utf-8")
    stack, roots = [], []

    def position():
        return len(utf8[:parser.CurrentByteIndex].decode("utf-8"))

    def start(tag, attrs):
        offset = position()
        opening = re.match(r"""<(?:"[^"]*"|'[^']*'|[^'">])*>""", text[offset:])
        if not opening:
            raise MigrationError("E_ENGINE_CONFIG", "invalid XML opening tag")
        open_end = offset + opening.end()
        node = _Node(tag, offset, open_end, opening.group().rstrip().endswith("/>"))
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    def end(tag):
        node = stack.pop()
        node.close_start = node.open_end if node.empty else position()
        if node.empty:
            node.end = node.open_end
        else:
            node.end = text.index(">", node.close_start) + 1

    def chars(value):
        if stack:
            stack[-1].text += value

    def reject_doctype(*args):
        raise MigrationError("E_ENGINE_CONFIG", "DTD configuration is unsupported")

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = chars
    parser.StartDoctypeDeclHandler = reject_doctype
    try:
        parser.Parse(text, True)
    except expat.ExpatError as exc:
        raise MigrationError("E_ENGINE_CONFIG", f"invalid XML: {exc}") from exc
    if len(roots) != 1 or roots[0].name != "Config":
        raise MigrationError("E_ENGINE_CONFIG", "Config root is missing")
    return roots[0]


def _one(parent: _Node, name: str) -> _Node | None:
    nodes = parent.named(name)
    if len(nodes) > 1:
        raise MigrationError("E_ENGINE_CONFIG", f"ambiguous {name} elements")
    return nodes[0] if nodes else None


def _basename(value: str) -> str:
    return value.strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()


def _all_nodes(node: _Node):
    yield node
    for child in node.children:
        yield from _all_nodes(child)


def _fresh_ids(root: _Node) -> tuple[str, str]:
    used = {node.text.strip().casefold() for node in _all_nodes(root)
            if node.name in {"Guid", "EngineGuid"}}
    engine_guid, setting_guid = ACE_GUID, ACE_SETTING_GUID
    counter = 0
    while engine_guid in used or setting_guid in used:
        counter += 1
        engine_guid = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                    f"pengfei-jieqi/{ENGINE_NAME}/{counter}"))
        setting_guid = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                     f"pengfei-jieqi/{ENGINE_NAME}/setting/{counter}"))
    return engine_guid, setting_guid


def _engine_item(nl: str, prefix: str, guid: str) -> str:
    fields = [("Guid", guid), ("Name", ENGINE_NAME),
              ("FilePath", BRIDGE_CONFIG_PATH), ("Arguments", None),
              ("EngineType", "UCI_PF"), ("Options", None),
              ("LastGetOptionsTime", "01/01/0001 00:00:00")]
    return _item("EngineConfigItem", fields, nl, prefix)


def _engine_setting(nl: str, prefix: str, guid: str, setting_guid: str) -> str:
    fields = [("IsChecked", "true"), ("EngineGuid", guid),
              ("ThreadNumber", "4"), ("HashSize", "256"),
              ("ColorString", "0 0 240"), ("Guid", setting_guid)]
    return _item("EngineSetting", fields, nl, prefix)


def _item(name: str, fields: list, nl: str, prefix: str) -> str:
    lines = [f"    <{prefix}{name}>"]
    for key, value in fields:
        lines.append(f"      <{prefix}{key} />" if value is None else
                     f"      <{prefix}{key}>{value}</{prefix}{key}>")
    lines.append(f"    </{prefix}{name}>")
    return nl.join(lines)


def _collection_edit(text: str, root: _Node, name: str, item: str,
                     nl: str, prefix: str) -> tuple[int, int, str]:
    node = _one(root, name)
    if node is None:
        section = f"{nl}  <{prefix}{name}>{nl}{item}{nl}  </{prefix}{name}>{nl}"
        return root.close_start, root.close_start, section
    if node.empty:
        opening = re.sub(r"/\s*>$", ">", text[node.start:node.open_end])
        replacement = opening + nl + item + nl + f"  </{node.tag}>"
        return node.start, node.end, replacement
    return node.close_start, node.close_start, nl + item + nl + "  "


def _rewrite_config(path: Path) -> tuple[bytes, bool]:
    raw = path.read_bytes()
    text, encoding = _decode(raw)
    root = _parse(text)
    if root.empty:
        raise MigrationError("E_ENGINE_CONFIG", "Config root has no content")
    engines = _one(root, "Engines")
    selected = []
    if engines:
        for item in engines.named("EngineConfigItem"):
            name, filepath = _one(item, "Name"), _one(item, "FilePath")
            if ((name and name.text.strip().casefold() == ENGINE_NAME.casefold()) or
                    (filepath and _basename(filepath.text) in
                     {ENGINE_NAME.casefold(), BRIDGE_NAME.casefold()})):
                selected.append((item, filepath))

    edits = []
    if selected:
        for item, filepath in selected:
            if filepath is None or filepath.children:
                raise MigrationError("E_ENGINE_CONFIG", "ACE FilePath is missing or complex")
            if filepath.text == BRIDGE_CONFIG_PATH:
                continue
            if filepath.empty:
                opening = re.sub(r"/\s*>$", ">", text[filepath.start:filepath.open_end])
                edits.append((filepath.start, filepath.end,
                              opening + BRIDGE_CONFIG_PATH + f"</{filepath.tag}>"))
            else:
                edits.append((filepath.open_end, filepath.close_start, BRIDGE_CONFIG_PATH))
    else:
        nl = "\r\n" if "\r\n" in text else "\n"
        prefix = root.tag.rsplit(":", 1)[0] + ":" if ":" in root.tag else ""
        guid, setting_guid = _fresh_ids(root)
        edits.append(_collection_edit(text, root, "Engines",
                                      _engine_item(nl, prefix, guid), nl, prefix))
        edits.append(_collection_edit(text, root, "EngineSettings",
                                      _engine_setting(nl, prefix, guid, setting_guid),
                                      nl, prefix))
    if not edits:
        return raw, False
    # Apply same-position insertions in reverse so the final schema order is
    # Engines followed by EngineSettings, as in the application's own config.
    for _index, (start, end, replacement) in sorted(
            enumerate(edits), key=lambda item: (item[1][0], item[0]), reverse=True):
        text = text[:start] + replacement + text[end:]
    _parse(text)
    return text.encode(encoding), True


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _backup(path: Path, data: bytes, backup_dir: Path) -> Path:
    digest = hashlib.sha256(data).hexdigest()
    destination = backup_dir / f"{path.name}.{digest}.orig"
    if destination.exists():
        if destination.read_bytes() != data:
            raise MigrationError("E_ENGINE_BACKUP", f"backup collision: {destination.name}")
        return destination
    _atomic_write(destination, data)
    return destination


def _copy_bridge(source: Path, destination: Path) -> bool:
    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file():
        raise MigrationError("E_BRIDGE_RESOURCE", f"missing bundled bridge: {source}")
    if source == destination:
        return False
    if destination.is_file() and destination.read_bytes() == source.read_bytes():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent))
    try:
        with os.fdopen(fd, "wb") as stream, source.open("rb") as original:
            shutil.copyfileobj(original, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return True


def install_engine_bridge(app_dir: Path, data_dir: Path, bridge_resource: Path) -> dict:
    """Install the bridge and migrate config files in a private App copy.

    Every validation occurs before a config write.  Original config bytes are
    content-addressed in ``backups/engine-bridge`` so a restart is idempotent
    and a later migration never overwrites a prior user's backup.
    """
    app_dir = Path(app_dir).resolve()
    data_dir = Path(data_dir).resolve()
    bundled = Path(bridge_resource).resolve()
    if bundled.is_dir() and (bundled / BRIDGE_NAME).is_file():
        bundled = bundled / BRIDGE_NAME
    elif bundled.name.casefold() != BRIDGE_NAME.casefold():
        bundled = bundled / "engine_bridge" / BRIDGE_NAME
    if not bundled.is_file():
        raise MigrationError("E_BRIDGE_RESOURCE", str(bundled))

    engine = app_dir / "Engine" / ENGINE_NAME
    if not engine.is_file() or _sha256(engine).casefold() != ENGINE_SHA256:
        raise MigrationError("E_ENGINE_VERSION", str(engine))

    config = app_dir / "Bin" / "config.myconfig"
    if not config.is_file():
        raise MigrationError("E_ENGINE_CONFIG", f"missing {config}")
    config_paths = [config]
    backup = config.with_name(config.name + ".bak")
    if backup.is_file():
        config_paths.append(backup)

    prepared: list[tuple[Path, bytes, bytes, bool]] = []
    for path in config_paths:
        original = path.read_bytes()
        updated, changed = _rewrite_config(path)
        prepared.append((path, original, updated, changed))

    backup_dir = data_dir / "backups" / "engine-bridge"
    backups: list[Path] = []
    for path, original, _updated, changed in prepared:
        if changed:
            backups.append(_backup(path, original, backup_dir))

    bridge_destination = app_dir / "Engine" / BRIDGE_NAME
    bridge_changed = _copy_bridge(bundled, bridge_destination)
    for path, _original, updated, changed in prepared:
        if changed:
            _atomic_write(path, updated)

    return {
        "bridge_path": str(bridge_destination),
        "bridge_installed": True,
        "bridge_changed": bridge_changed,
        "config_changed": any(item[3] for item in prepared),
        "configs": [str(item[0]) for item in prepared],
        "backups": [str(path) for path in backups],
        "engine_sha256": ENGINE_SHA256,
    }

