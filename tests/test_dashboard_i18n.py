"""
Dashboard translations: every t("...") in the dashboard has a Korean entry with
the same placeholders, no Korean is hard-coded outside the translation table,
and t() switches with the language.

    python3 -m unittest tests.test_dashboard_i18n -v
"""

import ast
import re
import string
import unittest
from pathlib import Path
from unittest import mock

from pumpfun.dashboard import i18n
from pumpfun.dashboard.ko import KO

DASHBOARD = Path(__file__).resolve().parent.parent / "pumpfun" / "dashboard"
HANGUL = re.compile(r"[가-힣]")


def t_calls():
    """(file, line, text) for every t("literal") in the dashboard, and non-literal calls as text None."""
    out = []
    for f in sorted(DASHBOARD.glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text())):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "t":
                arg = node.args[0] if node.args else None
                text = arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
                out.append((f.name, node.lineno, text))
    return out


def fields(text: str) -> set:
    return {(name, spec) for _, name, spec, _ in string.Formatter().parse(text) if name is not None}


class Translations(unittest.TestCase):
    def test_every_call_is_a_literal_with_a_korean_entry(self):
        calls = t_calls()
        self.assertGreater(len(calls), 100)
        non_literal = [(f, line) for f, line, text in calls if text is None]
        self.assertEqual(non_literal, [], "t() needs a string literal so it can be checked")
        missing = sorted({text for _, _, text in calls if text not in KO})
        self.assertEqual(missing, [])

    def test_placeholders_match(self):
        mismatched = [en for en, ko in KO.items() if fields(en) != fields(ko)]
        self.assertEqual(mismatched, [])

    def test_no_unused_entries(self):
        used = {text for _, _, text in t_calls()}
        self.assertEqual(sorted(set(KO) - used), [])

    def test_no_korean_outside_the_table(self):
        allowed = {"ko.py": None, "i18n.py": {"한국어", "언어"}}
        found = []
        for f in sorted(DASHBOARD.glob("*.py")):
            if f.name == "ko.py":
                continue
            for n, line in enumerate(f.read_text().splitlines(), 1):
                words = set(re.findall(r"[가-힣]+", line))
                if words - allowed.get(f.name, set()):
                    found.append(f"{f.name}:{n}")
        self.assertEqual(found, [])

    def test_t_switches_language(self):
        with mock.patch.object(i18n, "lang", return_value="en"):
            self.assertEqual(i18n.t("Coins"), "Coins")
            self.assertEqual(i18n.t("{n:.0f} min ago", n=3.2), "3 min ago")
        with mock.patch.object(i18n, "lang", return_value="ko"):
            self.assertEqual(i18n.t("Coins"), "코인")
            self.assertEqual(i18n.t("{n:.0f} min ago", n=3.2), "3분 전")
            self.assertEqual(i18n.t("text with no entry"), "text with no entry")


if __name__ == "__main__":
    unittest.main()
