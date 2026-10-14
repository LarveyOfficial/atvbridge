"""Apple TV text input payloads."""

from __future__ import annotations

import plistlib
import unittest

from pyatv.protocols.companion.plist_payloads.rti_text_operations import get_rti_input_text_payload

from atvbridge import rti

SESSION = bytes(range(16))


class RtiTest(unittest.TestCase):
    def test_insert_matches_pyatv_byte_for_byte(self) -> None:
        self.assertEqual(rti.text_operation_payload(SESSION, insert="hello"),
                         get_rti_input_text_payload(SESSION, "hello"))

    def test_delete_and_insert(self) -> None:
        archive = plistlib.loads(rti.text_operation_payload(SESSION, insert="ing", delete=2))
        objs = archive["$objects"]
        ops = objs[archive["$top"]["textOperations"].data]
        output = objs[ops["keyboardOutput"].data]
        self.assertEqual(output["deletionCount"], 2)
        self.assertEqual(objs[output["insertionText"].data], "ing")
        self.assertEqual(objs[output["$class"].data]["$classname"], "TIKeyboardOutput")
        self.assertEqual(objs[ops["targetSessionUUID"].data]["NS.uuidbytes"], SESSION)

    def test_diff(self) -> None:
        self.assertEqual(rti.diff("stra", "stranger"), (0, "nger"))
        self.assertEqual(rti.diff("stranger", "strange"), (1, ""))
        self.assertEqual(rti.diff("the offce", "the office"), (2, "ice"))
        self.assertEqual(rti.diff("abc", "abc"), (0, ""))
        self.assertEqual(rti.diff("", "x"), (0, "x"))


if __name__ == "__main__":
    unittest.main()
