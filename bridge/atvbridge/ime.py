"""Keyboard (IME) messages of the Android TV Remote protocol, encoded by hand.

androidtvremote2's remotemessage.proto mislabels these fields (its RemoteImeKeyInject /
RemoteImeBatchEdit / RemoteImeShowRequest), and cannot parse the phone's selection edits.
This layout follows Google's Android TV Remote Service (com.google.android.tv.remote.service,
ImeFeature / ImeBridgeService), which forwards Android's own EditorInfo / ExtractedText:

TV -> phone (RemoteMessage field):
  20 StartInput  {1: EditorInfo, 2: ExtractedText}   a field gained focus (or app changed)
  21 ShowHide    {1: bool shown, 2: int flags}       the TV's keyboard window opened/closed
  22 TextUpdate  {1: bool own_edit, 2: ExtractedText}
  23 Selection   {2: int start, 3: int end}
Phone -> TV:
  21 BatchEdit   {1: field id, 2: text counter, 3: repeated Edit}
       Edit oneof {1: Selection{2: start, 3: end}, 2: Replace{1: start, 2: end, 3: text},
                   5: EditorAction{1: action id}}
  22 ShowHide request

EditorInfo: 1 field id, 2 inputType, 3 imeOptions, 4 privateImeOptions, 5 actionLabel,
  6 actionId, 7 initialSelStart, 8 initialSelEnd, 9 initialCapsMode, 10 hintText, 11 label,
  12 packageName, 13 fieldId, 14 fieldName.
ExtractedText: 1 text counter, 2 text, 3 selectionStart, 4 selectionEnd, 5 flags, 6 hint.
"""

from __future__ import annotations

from dataclasses import dataclass

START_INPUT = 20
SHOW_HIDE = 21
TEXT_UPDATE = 22
SELECTION = 23
BATCH_EDIT = 21  # phone -> TV

TYPE_CLASS_TEXT = 1
IME_ACTION_SEARCH = 3


# --- tiny protobuf writer/reader ---------------------------------------------------------

def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _int(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def _bytes(field: int, data: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(data)) + data


def _str(field: int, value: str) -> bytes:
    return _bytes(field, value.encode())


def read_fields(data: bytes) -> list[tuple[int, int | bytes]]:
    """Top-level (field number, value) pairs: ints for varints, bytes for length-delimited."""
    out: list[tuple[int, int | bytes]] = []
    pos = 0

    def varint() -> int:
        nonlocal pos
        result = shift = 0
        while True:
            if pos >= len(data):
                raise ValueError("truncated varint")
            b = data[pos]
            pos += 1
            result |= (b & 0x7F) << shift
            if not b & 0x80:
                return result
            shift += 7

    while pos < len(data):
        key = varint()
        field, wire = key >> 3, key & 7
        if wire == 0:
            out.append((field, varint()))
        elif wire == 2:
            n = varint()
            if pos + n > len(data):
                raise ValueError("truncated field")
            out.append((field, data[pos:pos + n]))
            pos += n
        elif wire == 5:
            out.append((field, int.from_bytes(data[pos:pos + 4], "little")))
            pos += 4
        elif wire == 1:
            out.append((field, int.from_bytes(data[pos:pos + 8], "little")))
            pos += 8
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return out


def top_field(frame: bytes) -> int | None:
    """Field number of a RemoteMessage frame's (single) top-level field."""
    try:
        fields = read_fields(frame)
    except ValueError:
        return None
    return fields[0][0] if fields else None


# --- TV -> phone ---------------------------------------------------------------------------

def extracted_text(counter: int, text: str, sel_start: int, sel_end: int) -> bytes:
    return _int(1, counter) + _str(2, text) + _int(3, sel_start) + _int(4, sel_end)


def start_input(field_id: int, package: str, text_counter: int = 0, text: str | None = None,
                input_type: int = TYPE_CLASS_TEXT, ime_options: int = IME_ACTION_SEARCH,
                hint: str = "") -> bytes:
    """StartInput for a focused text field; field_id 0 = no text field (just the app)."""
    if field_id:
        info = _int(1, field_id) + _int(2, input_type)
        if ime_options:
            info += _int(3, ime_options)
        if text is not None:
            info += _int(7, len(text)) + _int(8, len(text))
        if hint:
            info += _str(10, hint)
        info += _str(12, package)
    else:
        info = _str(12, package)
    body = _bytes(1, info)
    if field_id and text is not None:
        body += _bytes(2, extracted_text(text_counter, text, len(text), len(text)))
    return _bytes(START_INPUT, body)


def show_hide(shown: bool, flags: int = 0) -> bytes:
    body = _int(1, int(shown))
    if flags:
        body += _int(2, flags)
    return _bytes(SHOW_HIDE, body)


def text_update(counter: int, text: str, sel_start: int, sel_end: int, own_edit: bool = False) -> bytes:
    body = (_int(1, 1) if own_edit else b"") + _bytes(2, extracted_text(counter, text, sel_start, sel_end))
    return _bytes(TEXT_UPDATE, body)


# --- phone -> TV ---------------------------------------------------------------------------

@dataclass
class Edit:
    kind: str  # "replace", "select" or "action"
    start: int = 0
    end: int = 0
    text: str = ""
    action: int = 0


@dataclass
class BatchEdit:
    field_id: int
    text_counter: int
    edits: list[Edit]


def parse_batch_edit(frame: bytes) -> BatchEdit:
    """Parse a whole RemoteMessage frame whose top-level field is 21 (BatchEdit)."""
    (field, body), = read_fields(frame)
    if field != BATCH_EDIT or not isinstance(body, bytes):
        raise ValueError("not a batch edit")
    field_id = counter = 0
    edits: list[Edit] = []
    for f, v in read_fields(body):
        if f == 1 and isinstance(v, int):
            field_id = v
        elif f == 2 and isinstance(v, int):
            counter = v
        elif f == 3 and isinstance(v, bytes):
            for kind, inner in read_fields(v):
                if not isinstance(inner, bytes):
                    continue
                vals = dict(read_fields(inner))
                if kind == 2:
                    raw = vals.get(3, b"")
                    edits.append(Edit("replace", int(vals.get(1, 0)), int(vals.get(2, 0)),
                                      raw.decode(errors="replace") if isinstance(raw, bytes) else ""))
                elif kind == 1:
                    edits.append(Edit("select", int(vals.get(2, 0)), int(vals.get(3, 0))))
                elif kind == 5:
                    edits.append(Edit("action", action=int(vals.get(1, 0))))
    return BatchEdit(field_id, counter, edits)


def apply_edits(text: str, cursor: tuple[int, int], edits: list[Edit]) -> tuple[str, tuple[int, int]]:
    """Apply edits like Android's setComposingRegion + commitText / setSelection."""
    for e in edits:
        if e.kind == "replace":
            start, end = sorted((min(e.start, len(text)), min(e.end, len(text))))
            text = text[:start] + e.text + text[end:]
            cursor = (start + len(e.text),) * 2  # type: ignore[assignment]
        elif e.kind == "select":
            cursor = (min(e.start, len(text)), min(e.end, len(text)))
    return text, cursor
