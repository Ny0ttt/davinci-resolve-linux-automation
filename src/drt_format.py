"""
Low-level pieces of DaVinci Resolve's .drt timeline file (Resolve 21.1.1, DbPrjVer 17), as
measured from the author's own Resolve exports (docs/design-notes.md#animated-captions-via-drt, docs/design-notes.md#animated-captions-via-drt). make_drt.py builds
timelines from these; nothing here knows about cutlists.

A .drt is a zip of XML (project.xml, MediaPool/Master/MpFolder.xml, SeqContainer/<id>.xml).
Binary values are hex inside the XML, in three encodings:

  envelope   00000002 + uint32 BE len(rest) + rest; rest = 0x81 + zstd frame (with content
             size -- Resolve can't read a frame without it) or 0x80 + raw bytes. Inside: protobuf.
  qmap       Qt QDataStream key/value maps (big-endian): 00000001 + count + (key, QVariant)...
             keys = uint32 byte length + UTF-16BE. Used for the media pool's Time / Geometry /
             audio tracks. Patched in place (same-size values only).
  text block the Text title's rich text (little-endian, inside the protobuf; see text_block).
"""
import struct
import subprocess

import resolve_read as rr


class DrtError(Exception):
    pass


# ------------------------------------------------------------------ envelope + protobuf

def unpack(blob):
    if blob[:4] != b"\x00\x00\x00\x02" or blob[8] not in (0x80, 0x81):
        raise DrtError("not a Resolve blob: %s" % blob[:9].hex())
    return blob[9:] if blob[8] == 0x80 else rr.unzstd(blob)


def pack(proto, compress=True):
    if compress:
        z = subprocess.run(["zstd", "-q", "-c", "-19", "--no-check", "--stream-size=%d" % len(proto)],
                           input=proto, capture_output=True, check=True).stdout
        if not z[4] & 0xC0 and not z[4] & 0x20:
            raise DrtError("zstd frame without content size")
        rest = b"\x81" + z
    else:
        rest = b"\x80" + proto
    return b"\x00\x00\x00\x02" + struct.pack(">I", len(rest)) + rest


def varint_bytes(n):
    out = bytearray()
    while True:
        x = n & 0x7F
        n >>= 7
        if n:
            out.append(x | 0x80)
        else:
            out.append(x)
            return bytes(out)


def split(b):
    """Lossless: [(field, wiretype, value)] -- int for varints, raw bytes otherwise."""
    out, i = [], 0
    while i < len(b):
        key, i = rr.varint(b, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = rr.varint(b, i)
        elif wt == 1:
            v, i = b[i:i + 8], i + 8
        elif wt == 2:
            n, i = rr.varint(b, i)
            v, i = b[i:i + n], i + n
        elif wt == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise DrtError("protobuf wire type %d" % wt)
        out.append((f, wt, v))
    return out


def join(fields):
    out = bytearray()
    for f, wt, v in fields:
        out += varint_bytes(f << 3 | wt)
        if wt == 0:
            out += varint_bytes(v)
        elif wt == 2:
            out += varint_bytes(len(v)) + v
        else:
            out += v
    return bytes(out)


def ld(field, payload):
    return (field, 2, payload)


def has_id(msg, ident):
    try:
        return any(f == 1 and wt == 0 and v == ident for f, wt, v in split(msg))
    except (IndexError, DrtError):
        return False


# ------------------------------------------------------------------ qmap (QDataStream)

def _qkey(name):
    s = name.encode("utf-16-be")
    return struct.pack(">I", len(s)) + s


def qmap_set(blob, key, fmt, value, nth=0):
    """Overwrite the value after `key` (QVariant: uint32 type + 1 null byte + value; a
    QByteArray value (type 12) has its own uint32 length first, which is kept)."""
    k = _qkey(key)
    i = -1
    for _ in range(nth + 1):
        i = blob.find(k, i + 1)
        if i < 0:
            raise DrtError("key %s not in blob" % key)
    at = i + len(k) + 5
    if struct.unpack(">I", blob[i + len(k):i + len(k) + 4])[0] == 12:
        at += 4
    size = struct.calcsize(fmt)
    old = blob[at:at + size]
    if len(old) != size:
        raise DrtError("key %s: value runs past the blob" % key)
    return blob[:at] + struct.pack(fmt, *value if isinstance(value, tuple) else (value,)) + blob[at + size:]


def qmap_get(blob, key, fmt, nth=0):
    k = _qkey(key)
    i = -1
    for _ in range(nth + 1):
        i = blob.find(k, i + 1)
        if i < 0:
            raise DrtError("key %s not in blob" % key)
    at = i + len(k) + 5
    if struct.unpack(">I", blob[i + len(k):i + len(k) + 4])[0] == 12:
        at += 4
    v = struct.unpack(fmt, blob[at:at + struct.calcsize(fmt)])
    return v if len(v) > 1 else v[0]


# ------------------------------------------------------------------ text block (Text title)

def _i32(n):
    return struct.pack("<i", n)


def _str16(s):
    b = s.encode("utf-16-le")
    return _i32(4 + len(b)) + b


def _read_str16(b, i):
    n = struct.unpack("<i", b[i:i + 4])[0]
    return b[i + 4:i + n].decode("utf-16-le"), i + n


def text_block_parts(blk):
    """Resolve's text block -> (lines, [style item bytes], run flags). Layout (all int32 LE):
        int32 total | 0b b1 00 00 | str16 item types ("b" + "a"*n + "`"*n) | items, each
        int32 len (counts itself) + body:
          b: int32 4+4n | int32 run item numbers 1..n | 38 04 00
          a: str16 the line | int32 its style item number | 2 bytes flags   (one per line)
          `: the style (font, size, weight, colour, spacing...)              (one per line)
    A line break is not a "\\n": every line is its own run with its own style."""
    if blk[4:8] != b"\x0b\xb1\x00\x00" or struct.unpack("<i", blk[:4])[0] != len(blk):
        raise DrtError("not a text block")
    types, i = _read_str16(blk, 8)
    items = []
    while i < len(blk):
        n = struct.unpack("<i", blk[i:i + 4])[0]
        items.append(blk[i:i + n])
        i += n
    if len(items) != len(types) or not types.startswith("b") or i != len(blk):
        raise DrtError("text block items don't match its type list %r" % types)
    n = types.count("a")
    if types != "b" + "a" * n + "`" * n:
        raise DrtError("unexpected text block layout %r" % types)
    lines, flags = [], None
    for it in items[1:1 + n]:
        s, j = _read_str16(it, 4)
        lines.append(s)
        flags = it[j + 4:j + 6]
    return lines, items[1 + n:], flags


def text_block(lines, style, flags=b"\x01\x00"):
    """Text block for 1+ lines, every line in `style` (one style item's bytes)."""
    n = len(lines)
    if not n:
        raise DrtError("a caption needs at least one line")
    types = "b" + "a" * n + "`" * n
    body_b = _i32(4 + 4 * n) + b"".join(_i32(k) for k in range(1, n + 1)) + b"\x38\x04\x00"
    items = [_i32(4 + len(body_b)) + body_b]
    for k, line in enumerate(lines):
        body = _str16(line) + _i32(n + 1 + k) + flags
        items.append(_i32(4 + len(body)) + body)
    items += [style] * n
    rest = b"\x0b\xb1\x00\x00" + _str16(types) + b"".join(items)
    return _i32(4 + len(rest)) + rest


def style_size(style):
    """Font size: the float after the font name (style = int32 len | str16 font | float32 size ...)."""
    _, j = _read_str16(style, 4)
    return struct.unpack("<f", style[j:j + 4])[0]


def set_style_size(style, size):
    _, j = _read_str16(style, 4)
    return style[:j] + struct.pack("<f", size) + style[j + 4:]


# EffectFiltersBA protobuf -> filter (1) with id 48 -> slot (9) id 15 -> fields 3 + 4 -> 1 -> 8
TEXT_FILTER, TEXT_SLOT, TRANSFORM_FILTER = 48, 15, 4


def _edit(msg, rules, fn):
    if not rules:
        return fn(msg)
    nums, pred = rules[0]
    return join([(f, wt, _edit(v, rules[1:], fn) if wt == 2 and f in nums and pred(v) else v)
                 for f, wt, v in split(msg)])


TEXT_PATH = [((1,), lambda m: has_id(m, TEXT_FILTER)), ((9,), lambda m: has_id(m, TEXT_SLOT)),
             ((3, 4), lambda m: True), ((1,), lambda m: True), ((8,), lambda m: True)]


def map_text(proto, fn):
    return _edit(proto, TEXT_PATH, fn)


def get_text_block(proto):
    found = []
    map_text(proto, lambda b: found.append(b) or b)
    if not found:
        raise DrtError("no text in this title")
    return found[0]


def set_text_block(proto, blk):
    return map_text(proto, lambda _: blk)


# ------------------------------------------------------------------ transform filter (id 4)

def filters(proto):
    """Top-level filters as raw field tuples."""
    return split(proto)


def is_filter(field, ident):
    f, wt, v = field
    return f == 1 and wt == 2 and has_id(v, ident)


def transform_slots(tf):
    """{param id: raw slot bytes} of a transform filter body."""
    out = {}
    for f, wt, v in split(tf):
        if f == 9 and wt == 2:
            pid = next((x for ff, ww, x in split(v) if ff == 1 and ww == 0), None)
            out[pid] = v
    return out


def _set_slot_value(slot, value):
    """A slot's static value lives in fields 3 and 4 -> 1 -> 2 (double)."""
    def inner(m):
        return join([(f, wt, struct.pack("<d", value)) if f == 2 and wt == 1 else (f, wt, v)
                     for f, wt, v in split(m)])

    def outer(m):
        return join([(f, wt, inner(v)) if f == 1 and wt == 2 else (f, wt, v) for f, wt, v in split(m)])
    return join([(f, wt, outer(v)) if f in (3, 4) and wt == 2 else (f, wt, v) for f, wt, v in split(slot)])


def set_transform(tf, values, keys=None):
    """Set static params ({id: value}) and optionally keyframes ({id: [(frame, value)]}) on a
    transform filter body. Every param must already have a slot (Resolve only writes the ones
    that aren't default; a template supplies them)."""
    keys = keys or {}
    have = transform_slots(tf)
    missing = [p for p in list(values) + list(keys) if p not in have]
    if missing:
        raise DrtError("transform has no slot for param(s) %s" % missing)
    out = []
    for f, wt, v in split(tf):
        if f == 9 and wt == 2:
            pid = next((x for ff, ww, x in split(v) if ff == 1 and ww == 0), None)
            if pid in keys:
                v = _set_slot_value(v, keys[pid][0][1])
                v = join([(ff, ww, keylist(keys[pid])) if ff == 10 and ww == 2 else (ff, ww, x)
                          for ff, ww, x in split(v)])
                if not any(ff == 10 for ff, _, _ in split(v)):
                    raise DrtError("param %s has no keyframe list to replace" % pid)
            elif pid in values:
                v = _set_slot_value(v, values[pid])
        out.append((f, wt, v))
    return join(out)


def keylist(keys):
    """Keyframe list (slot field 10): int32 BE -(16 n), then per key int32 LE frame (from the
    item's first frame), u16 0, u16 0x000c, double LE value."""
    return struct.pack(">i", -16 * len(keys)) + b"".join(
        struct.pack("<iHHd", int(f), 0, 0x0C, float(v)) for f, v in keys)


def read_keys(slot):
    for f, wt, v in split(slot):
        if f == 10 and wt == 2:
            n = -struct.unpack(">i", v[:4])[0] // 16
            return [struct.unpack("<iHHd", v[4 + 16 * k:20 + 16 * k]) for k in range(n)]
    return []


def static_value(slot):
    for f, wt, v in split(slot):
        if f == 3 and wt == 2:
            for f2, w2, v2 in split(v):
                if f2 == 1 and w2 == 2:
                    for f3, w3, v3 in split(v2):
                        if f3 == 2 and w3 == 1:
                            return struct.unpack("<d", v3)[0]
    return None


def replace_filter(proto, ident, new_body):
    """Drop filter `ident` (if any) and append `new_body` as that filter."""
    kept = [x for x in split(proto) if not is_filter(x, ident)]
    return join(kept + [ld(1, new_body)])


def get_filter(proto, ident):
    for x in split(proto):
        if is_filter(x, ident):
            return x[2]
    return None
