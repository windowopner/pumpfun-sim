"""
Dashboard language. English is the default; Korean is picked in the sidebar,
with `pumpfun dashboard --lang ko`, or with $PUMPFUN_LANG=ko.

The English text in the code is the key: t("Coins") returns "Coins" in English
and KO["Coins"] in Korean. Placeholders use str.format: t("{n:,} coins", n=5).
tests/test_dashboard_i18n.py checks that every t("...") in the dashboard has a
Korean entry with the same placeholders.
"""

import os

import streamlit as st

from pumpfun.dashboard.ko import KO

LANGUAGES = {"en": "English", "ko": "한국어"}


def lang() -> str:
    chosen = st.session_state.get("lang")
    if chosen in LANGUAGES:
        return chosen
    env = os.environ.get("PUMPFUN_LANG", "en")
    return env if env in LANGUAGES else "en"


def t(text: str, **values) -> str:
    out = KO.get(text, text) if lang() == "ko" else text
    return out.format(**values) if values else out


def language_picker():
    current = lang()
    choice = st.radio("Language / 언어", list(LANGUAGES), index=list(LANGUAGES).index(current),
                      format_func=LANGUAGES.get, horizontal=True)
    st.session_state["lang"] = choice
