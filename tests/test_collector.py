"""
Tests against real mainnet transactions saved in tests/fixtures
(fetched 2026-10-15 with getTransaction).

    python3 -m unittest discover tests
"""

import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pumpfun.collect.idl_decoder import IdlEventDecoder
from pumpfun.paths import IDL_PATH
from pumpfun.collect.logs import program_data
from pumpfun.collect.store import EventStore

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
SOL_QUOTE = "11111111111111111111111111111111"  # Pubkey::default() means SOL-paired


def load(name):
    return json.loads((FIXTURES / name).read_text())


def ceil_div(a, b):
    return -(-a // b)


class DecodeRealTransactions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.decoder = IdlEventDecoder.from_file(IDL_PATH)

    def events(self, fixture):
        tx = load(fixture)
        return [self.decoder.decode(p) for p in program_data(tx["logs"], PUMP)]

    def test_create_v2_then_first_buy(self):
        create, trade = self.events("create_tx.json")
        self.assertEqual(create.name, "CreateEvent")
        self.assertEqual(create.missing_fields, [])
        self.assertEqual(create.trailing_bytes, 0)
        c = create.fields
        self.assertEqual(c["symbol"], "ART")
        self.assertEqual(c["mint"], "2w5u3rHNBXnPZRsg9x2MdCiX31rGQMXy25wENAbJZD4v")
        self.assertEqual(c["token_program"], "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb")
        self.assertEqual(c["quote_mint"], SOL_QUOTE)
        self.assertEqual(c["virtual_token_reserves"], 1_073_000_000_000_000)
        self.assertEqual(c["virtual_sol_reserves"], 30_000_000_000)
        self.assertEqual(c["token_total_supply"], 1_000_000_000_000_000)

        self.assertEqual(trade.name, "TradeEvent")
        t = trade.fields
        self.assertEqual(t["mint"], c["mint"])
        self.assertTrue(t["is_buy"])
        self.assertEqual(t["ix_name"], "buy")

        # The event reports reserves after the trade; the curve started at the create values.
        self.assertEqual(t["virtual_sol_reserves"], c["virtual_sol_reserves"] + t["sol_amount"])
        self.assertEqual(t["virtual_token_reserves"], c["virtual_token_reserves"] - t["token_amount"])

        # Bonding curve buy cost for an exact token amount (pump-sdk getBuySolAmountFromTokenAmountQuote).
        vq, vt, amount = c["virtual_sol_reserves"], c["virtual_token_reserves"], t["token_amount"]
        self.assertEqual(t["sol_amount"], amount * vq // (vt - amount) + 1)

        # Fees are rounded up, on top of the curve cost.
        self.assertEqual(t["fee_basis_points"], 95)
        self.assertEqual(t["creator_fee_basis_points"], 30)
        self.assertEqual(t["fee"], ceil_div(t["sol_amount"] * 95, 10_000))
        self.assertEqual(t["creator_fee"], ceil_div(t["sol_amount"] * 30, 10_000))

    def test_buy_exact_sol_in(self):
        (trade,) = self.events("trade_tx.json")
        t = trade.fields
        self.assertEqual(trade.name, "TradeEvent")
        self.assertEqual(t["ix_name"], "buy_exact_sol_in")
        self.assertEqual(t["mint"], "FUx7DeWXoQLVPfa9C9bfL6So52xqJ1qrVNnTaLTXpump")
        self.assertEqual(t["quote_mint"], SOL_QUOTE)
        self.assertEqual(t["fee"], ceil_div(t["sol_amount"] * t["fee_basis_points"], 10_000))


class ProgramDataAttribution(unittest.TestCase):
    def test_only_payloads_emitted_by_the_target_program(self):
        mine, other = base64.b64encode(b"mine").decode(), base64.b64encode(b"other").decode()
        logs = [
            f"Program {PUMP} invoke [1]",
            f"Program data: {mine}",
            "Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA invoke [2]",
            "Program log: success",                       # a log line, not a frame
            f"Program data: {other}",
            "Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA consumed 10 of 100 compute units",
            "Program pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA success",
            f"Program data: {mine}",
            f"Program {PUMP} success",
            f"Program data: {other}",                     # outside any frame
        ]
        self.assertEqual(program_data(logs, PUMP), [b"mine", b"mine"])


class DecoderLayoutChanges(unittest.TestCase):
    IDL = {
        "address": "x",
        "events": [{"name": "Ev", "discriminator": [1, 2, 3, 4, 5, 6, 7, 8]}],
        "types": [{"name": "Ev", "type": {"kind": "struct", "fields": [
            {"name": "a", "type": "u64"},
            {"name": "b", "type": "bool"},
            {"name": "c", "type": "i128"},
        ]}}],
    }
    DISC = bytes([1, 2, 3, 4, 5, 6, 7, 8])

    def test_older_shorter_event_reads_missing_fields_as_none(self):
        ev = IdlEventDecoder(self.IDL).decode(self.DISC + (7).to_bytes(8, "little") + b"\x01")
        self.assertEqual(ev.fields, {"a": 7, "b": True, "c": None})
        self.assertEqual(ev.missing_fields, ["c"])

    def test_newer_longer_event_reports_trailing_bytes(self):
        data = self.DISC + (7).to_bytes(8, "little") + b"\x00" + (-5).to_bytes(16, "little", signed=True) + b"xyz"
        ev = IdlEventDecoder(self.IDL).decode(data)
        self.assertEqual(ev.fields["c"], -5)
        self.assertEqual(ev.trailing_bytes, 3)

    def test_event_cut_inside_a_field_is_an_error(self):
        with self.assertRaises(ValueError):
            IdlEventDecoder(self.IDL).decode(self.DISC + b"\x07\x00")

    def test_unknown_discriminator(self):
        self.assertIsNone(IdlEventDecoder(self.IDL).decode(b"\x00" * 16))


class Store(unittest.TestCase):
    def test_duplicates_ignored_and_big_ints_kept_exact(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            store = EventStore(path)
            fields = {"mint": "M", "is_buy": True, "user": "U", "timestamp": 1,
                      "token_amount": 2**64 - 1, "sol_amount": 5}
            self.assertTrue(store.add_event("TradeEvent", fields, "sig", 0, 10))
            self.assertFalse(store.add_event("TradeEvent", fields, "sig", 0, 10))
            self.assertTrue(store.add_event("TradeEvent", fields, "sig", 1, 10))
            self.assertFalse(store.add_event("SweepBondingCurveFeeEvent", fields, "sig", 2, 10))
            store.close()
            rows = sqlite3.connect(str(path)).execute(
                "SELECT event_index, token_amount, sol_amount, is_buy FROM trades ORDER BY event_index").fetchall()
            self.assertEqual(rows, [(0, str(2**64 - 1), 5, 1), (1, str(2**64 - 1), 5, 1)])


if __name__ == "__main__":
    unittest.main()
