"""Apple TV remote text input (RTI) operations over Companion.

pyatv's keyboard calls restart the text input session and, for text_set, clear the field and
re-type everything, which visibly flickers when done per keystroke. Here we keep one session
per focused field and send only the change: TIKeyboardOutput's deletionCount (backspaces at
the cursor) plus insertionText. The archive layout matches pyatv's
plist_payloads/rti_text_operations.py, with deletionCount added.
"""

from __future__ import annotations

import plistlib


def text_operation_payload(session_uuid: bytes, insert: str = "", delete: int = 0) -> bytes:
    """NSKeyedArchiver (RTIKeyedArchiver) payload for one RTITextOperations."""
    objects: list = ["$null"]

    def add(obj: object) -> plistlib.UID:
        objects.append(obj)
        return plistlib.UID(len(objects) - 1)

    operations: dict = {}
    output: dict = {}
    add(operations)  # 1
    output_uid = add(output)  # 2
    operations["keyboardOutput"] = output_uid
    if insert:
        output["insertionText"] = add(insert)
    if delete:
        output["deletionCount"] = delete
    output["$class"] = add({"$classname": "TIKeyboardOutput", "$classes": ["TIKeyboardOutput", "NSObject"]})
    uuid_class = None
    uuid = {"NS.uuidbytes": session_uuid}
    uuid_uid = add(uuid)
    uuid_class = add({"$classname": "NSUUID", "$classes": ["NSUUID", "NSObject"]})
    uuid["$class"] = uuid_class
    operations["$class"] = add({"$classname": "RTITextOperations", "$classes": ["RTITextOperations", "NSObject"]})
    operations["targetSessionUUID"] = uuid_uid
    # Key order as pyatv writes it (Apple's unarchiver doesn't care, but keeps payloads identical).
    ordered_ops = {k: operations[k] for k in ("keyboardOutput", "$class", "targetSessionUUID")}
    objects[1] = ordered_ops
    return plistlib.dumps(
        {"$version": 100000, "$archiver": "RTIKeyedArchiver", "$top": {"textOperations": plistlib.UID(1)},
         "$objects": objects},
        fmt=plistlib.PlistFormat.FMT_BINARY, sort_keys=False,
    )


def diff(old: str, new: str) -> tuple[int, str]:
    """(characters to delete from the end of `old`, text to append) to turn old into new.
    tvOS text fields keep the cursor at the end, so edits are expressed there."""
    prefix = 0
    for a, b in zip(old, new):
        if a != b:
            break
        prefix += 1
    return len(old) - prefix, new[prefix:]
