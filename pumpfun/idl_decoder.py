"""
Borsh decoder for Anchor events, driven by the program's IDL.

Anchor writes each event as 8 discriminator bytes followed by the Borsh
encoding of the event struct. Pump appends fields to its events over time, so
the decoder tolerates an event that ends early at a field boundary (the
missing trailing fields read as None) and one that carries extra bytes it
does not know about yet (reported in `trailing_bytes`).
"""

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

_INT_FORMATS = {
    "u8": "<B", "u16": "<H", "u32": "<I", "u64": "<Q",
    "i8": "<b", "i16": "<h", "i32": "<i", "i64": "<q",
}


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = B58_ALPHABET[rem] + out
    pad = len(data) - len(data.lstrip(b"\0"))
    return "1" * pad + out


class _OutOfData(Exception):
    """Raised when the buffer ends before a value is complete."""


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise _OutOfData()
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos


@dataclass
class DecodedEvent:
    name: str
    fields: Dict[str, Any]
    missing_fields: List[str] = field(default_factory=list)
    trailing_bytes: int = 0


class IdlEventDecoder:
    """
    Decodes `Program data:` payloads into named events.

    Usage:
        decoder = IdlEventDecoder.from_file("idl/pump.json")
        event = decoder.decode(base64.b64decode(payload))  # None if unknown
    """

    def __init__(self, idl: dict):
        self.address = idl.get("address")
        self._types = {t["name"]: t["type"] for t in idl.get("types", [])}
        self._events = {
            bytes(e["discriminator"]): e["name"] for e in idl.get("events", [])
        }

    @classmethod
    def from_file(cls, path) -> "IdlEventDecoder":
        return cls(json.loads(Path(path).read_text()))

    @property
    def event_names(self) -> List[str]:
        return sorted(self._events.values())

    def decode(self, data: bytes) -> Optional[DecodedEvent]:
        name = self._events.get(bytes(data[:8]))
        if name is None:
            return None
        reader = _Reader(data[8:])
        fields: Dict[str, Any] = {}
        missing: List[str] = []
        struct_fields = self._types[name]["fields"]
        for i, f in enumerate(struct_fields):
            if reader.remaining == 0:
                missing = [g["name"] for g in struct_fields[i:]]
                break
            try:
                fields[f["name"]] = self._read(reader, f["type"])
            except _OutOfData:
                # The event ended inside a field, so it is not a layout we know.
                raise ValueError(f"{name}: data ends inside field {f['name']!r}")
        for m in missing:
            fields[m] = None
        return DecodedEvent(name, fields, missing, reader.remaining)

    def _read(self, r: _Reader, t) -> Any:
        if isinstance(t, str):
            if t in _INT_FORMATS:
                fmt = _INT_FORMATS[t]
                return struct.unpack(fmt, r.take(struct.calcsize(fmt)))[0]
            if t == "u128":
                return int.from_bytes(r.take(16), "little")
            if t == "i128":
                return int.from_bytes(r.take(16), "little", signed=True)
            if t == "bool":
                return r.take(1)[0] != 0
            if t == "pubkey":
                return b58encode(r.take(32))
            if t == "string":
                n = struct.unpack("<I", r.take(4))[0]
                return r.take(n).decode("utf-8", errors="replace")
            if t == "bytes":
                n = struct.unpack("<I", r.take(4))[0]
                return r.take(n).hex()
            raise ValueError(f"unsupported IDL type {t!r}")
        if "option" in t:
            return self._read(r, t["option"]) if r.take(1)[0] else None
        if "vec" in t:
            n = struct.unpack("<I", r.take(4))[0]
            return [self._read(r, t["vec"]) for _ in range(n)]
        if "array" in t:
            inner, n = t["array"]
            return [self._read(r, inner) for _ in range(n)]
        if "defined" in t:
            return self._read_defined(r, t["defined"]["name"])
        raise ValueError(f"unsupported IDL type {t!r}")

    def _read_defined(self, r: _Reader, name: str) -> Any:
        ty = self._types[name]
        if ty["kind"] == "struct":
            return {f["name"]: self._read(r, f["type"]) for f in ty["fields"]}
        if ty["kind"] == "enum":
            variant = ty["variants"][r.take(1)[0]]
            vfields = variant.get("fields")
            if not vfields:
                return variant["name"]
            if isinstance(vfields[0], dict):
                return {variant["name"]: {f["name"]: self._read(r, f["type"]) for f in vfields}}
            return {variant["name"]: [self._read(r, f) for f in vfields]}
        raise ValueError(f"unsupported IDL type kind {ty['kind']!r}")
