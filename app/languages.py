"""The one place that knows which languages CareCall speaks.

Gnani Prisma (speech-to-text) and Timbre v2.5 (text-to-speech) cover the same
ten Indian languages, and Timbre adds a Hinglish profile. Everything else in
the app (voices, prompts, fixed replies, the browser's language picker) reads
this table, so adding a language means adding one row here plus its phrases.

Voices: one female voice per language, to keep the receptionist the same
"person" across languages. Poorvi (Hinglish) was picked by ear; the others
are the catalogue voices whose preferred language matches.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

AUTO = "auto"                 # picker choice: let Prisma work out the language
DEFAULT = "hi-en"
STT_AUTO_CODE = "indic-auto"  # Prisma v3.0 only
MAX_STT_CODES = 4             # Prisma accepts at most four codes per request


@dataclass(frozen=True)
class Language:
    code: str            # what the app, the LLM and the database use
    name: str            # English name
    native: str          # name in its own script, for the picker
    stt_code: str        # Prisma language code
    voice: str           # Timbre v2.5 voice
    tts_code: str        # Timbre `language` value
    script: str | None   # Unicode block of its script; None for Latin
    html_lang: str       # value for the HTML lang attribute
    enabled: bool = True


_DEVANAGARI = "ऀ-ॿ"

_ALL = (
    Language("hi-en", "Hinglish", "Hinglish", "hi-IN", "Poorvi", "hi-en", _DEVANAGARI, "hi"),
    Language("hi-IN", "Hindi", "हिंदी", "hi-IN", "Nalini", "hi-IN", _DEVANAGARI, "hi"),
    Language("en-IN", "English", "English", "en-IN", "Kaveri", "en-IN", None, "en"),
    Language("mr-IN", "Marathi", "मराठी", "mr-IN", "Zahira", "mr-IN", _DEVANAGARI, "mr"),
    Language("bn-IN", "Bengali", "বাংলা", "bn-IN", "Kirra", "bn-IN", "ঀ-৿", "bn"),
    Language("gu-IN", "Gujarati", "ગુજરાતી", "gu-IN", "Falak", "gu-IN", "઀-૿", "gu"),
    Language("pa-IN", "Punjabi", "ਪੰਜਾਬੀ", "pa-IN", "Mehuli", "pa-IN", "਀-੿", "pa"),
    Language("ta-IN", "Tamil", "தமிழ்", "ta-IN", "Trisha", "ta-IN", "஀-௿", "ta"),
    Language("te-IN", "Telugu", "తెలుగు", "te-IN", "Lavanya", "te-IN", "ఀ-౿", "te"),
    Language("kn-IN", "Kannada", "ಕನ್ನಡ", "kn-IN", "Saanvi", "kn-IN", "ಀ-೿", "kn"),
    Language("ml-IN", "Malayalam", "മലയാളം", "ml-IN", "Reshma", "ml-IN", "ഀ-ൿ", "ml"),
)

_BY_CODE = {lang.code: lang for lang in _ALL}

# The three the project started with. Their number and time wording is hand-written
# (see tts_text); the others rely on Timbre's own text normalisation.
CORE = ("hi-en", "hi-IN", "en-IN")

# Every Indic letter block from Devanagari to Malayalam, for word-splitting regexes.
INDIC_RANGE = "ऀ-ൿ"
_JOINERS = re.compile("[‌‍]")


def enabled() -> tuple[Language, ...]:
    return tuple(lang for lang in _ALL if lang.enabled)


def codes() -> tuple[str, ...]:
    return tuple(lang.code for lang in enabled())


def is_supported(code: str | None) -> bool:
    return code in _BY_CODE and _BY_CODE[code].enabled


def get(code: str | None) -> Language:
    """The language for `code`, or the default when it is unknown or switched off."""
    return _BY_CODE[code] if is_supported(code) else _BY_CODE[DEFAULT]


def stt_codes(code: str | None, auto: bool = False, core_default: str = "hi-IN,en-IN") -> str:
    """The `language_code` to send to Prisma for a call in this language.

    English is always listed as well, because callers drop English words into
    every Indian language ("appointment", "doctor", "cancel").
    """
    if auto:
        return STT_AUTO_CODE
    lang = get(code)
    if lang.code in CORE:
        return core_default
    return f"{lang.stt_code},en-IN"


def strip_joiners(text: str) -> str:
    """Remove zero-width joiners so the same word always compares equal."""
    return _JOINERS.sub("", text)


# ---------------------------------------------------------------- script detection

_SCRIPT_RE = {}
for _lang in _ALL:
    if _lang.script and _lang.script not in _SCRIPT_RE:
        _SCRIPT_RE[_lang.script] = re.compile(f"[{_lang.script}]")
# A script used by exactly one language identifies that language.
_UNIQUE = {}
for _script in _SCRIPT_RE:
    _owners = [lang.code for lang in _ALL if lang.script == _script]
    if len(_owners) == 1:
        _UNIQUE[_script] = _owners[0]


def dominant_script(text: str) -> str | None:
    """The Indic script with the most letters in `text`, or None if there are none."""
    best, best_count = None, 0
    for script, pattern in _SCRIPT_RE.items():
        count = len(pattern.findall(text))
        if count > best_count:
            best, best_count = script, count
    return best


def from_script(text: str) -> str | None:
    """Language code when the script alone settles it (everything except Devanagari)."""
    code = _UNIQUE.get(dominant_script(text) or "")
    return code if is_supported(code) else None


def has_devanagari(text: str) -> bool:
    return bool(_SCRIPT_RE[_DEVANAGARI].search(text))
