"""Deterministic safety and privacy rules that never depend on the LLM.

- EMERGENCY: red-flag symptoms -> stop, tell the caller to call 108/112, urgent ticket.
- MEDICAL:   requests for medicine, dosage, diagnosis or report interpretation -> refuse, urgent ticket.
- HUMAN:     the caller asks for a person -> callback ticket.
A plain symptom mentioned while booking ("bukhar hai, doctor ko dikhana hai")
is *not* escalated: booking a visit for a fever is exactly the clinic's job.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

EMERGENCY = [
    r"chest pain", r"pain in (my )?chest", r"seene? (me|mein|mai)n? (dard|pain)", r"सीने में (दर्द|भारीपन)",
    r"छाती में दर्द", r"chhati (me|mein) dard", r"heart attack", r"हार्ट अटैक", r"dil ka daura",
    r"can'?t breathe", r"cannot breathe", r"breathing (problem|difficulty|trouble)", r"short(ness)? of breath",
    r"saa?ns? (nahi|nahin|nhi|lene) ", r"saa?ns? (phool|fool)", r"सांस (नहीं|लेने में|फूल)", r"साँस (नहीं|लेने में|फूल)",
    r"unconscious", r"fainted", r"behosh", r"बेहोश", r"heavy bleeding", r"bleeding (a lot|heavily|nahi ruk)",
    r"khoon (beh|nahi ruk|bahut)", r"खून (बह|नहीं रुक)", r"stroke", r"lakwa", r"लकवा", r"paralys",
    r"seizure", r"fits? (aa|aaya|aaye)", r"दौरा पड़", r"mirgi", r"poison", r"zeher", r"ज़हर", r"जहर",
    r"suicid", r"kill myself", r"आत्महत्या", r"accident", r"एक्सीडेंट", r"दुर्घटना",
]

MEDICAL_ADVICE = [
    r"which (medicine|tablet|medication)", r"what (medicine|tablet|medication)", r"should i take",
    r"dos(e|age)", r"खुराक", r"kitni (goli|tablet|dawa)", r"कितनी (गोली|दवा)",
    r"kaun ?si (dawa|dawai|medicine|tablet|goli)", r"कौन ?सी (दवा|दवाई|गोली|टैबलेट)",
    r"(dawa|dawai|medicine|tablet|goli) (bata|batao|bataiye|de do|dijiye|lu|loon|lun|khau|khaun|le lu)",
    r"(दवा|दवाई|गोली) (बता|दे|लूँ|लूं|खाऊँ|खाऊं|ले लूँ)", r"prescri", r"diagnos",
    r"what does my report (mean|say)", r"report (ka matlab|normal hai kya|mein kya)", r"रिपोर्ट (का मतलब|नॉर्मल)",
    r"is (it|this) serious", r"kya (ye|yeh) serious hai", r"इलाज बता", r"ilaaj bata", r"treatment (bata|suggest)",
]

HUMAN = [
    r"\bhuman\b", r"real person", r"talk to (a|some)?\s*(person|someone|staff|receptionist)",
    r"speak (to|with) (a|the|some)?\s*(person|someone|staff|receptionist|human)", r"\bagent\b",
    r"insaa?n se", r"इंसान से", r"aadmi se baat", r"kisi se baat", r"staff se baat", r"स्टाफ से बात",
    r"receptionist se baat", r"रिसेप्शन(िस्ट)? से बात", r"reception se baat", r"call back (me|kar)", r"callback",
]

_EMERGENCY = [re.compile(p, re.I) for p in EMERGENCY]
_MEDICAL = [re.compile(p, re.I) for p in MEDICAL_ADVICE]
_HUMAN = [re.compile(p, re.I) for p in HUMAN]


@dataclass(frozen=True)
class SafetyResult:
    kind: str | None          # "EMERGENCY" | "MEDICAL" | "HUMAN" | None
    matched: str = ""


def _prep(text: str) -> str:
    return " " + unicodedata.normalize("NFC", text).lower().replace("’", "'") + " "


def check(text: str) -> SafetyResult:
    t = _prep(text)
    for kind, patterns in (("EMERGENCY", _EMERGENCY), ("MEDICAL", _MEDICAL), ("HUMAN", _HUMAN)):
        for p in patterns:
            m = p.search(t)
            if m:
                return SafetyResult(kind, m.group(0).strip())
    return SafetyResult(None)


# ---------------------------------------------------------------- replies (no LLM involved)

REPLIES = {
    "EMERGENCY": {
        "en-IN": "This sounds like an emergency. Please call 108 or 112 right now, or go to the nearest hospital. I have alerted our clinic staff to call you.",
        "hi-IN": "यह इमरजेंसी लग रही है। कृपया तुरंत 108 या 112 पर कॉल करें या नज़दीकी अस्पताल जाएँ। मैंने क्लिनिक स्टाफ को आपको कॉल करने के लिए कह दिया है।",
        "hi-en": "यह emergency लग रही है। Please तुरंत 108 या 112 पर call करें या नज़दीकी hospital जाएँ। मैंने clinic staff को आपको call करने के लिए बोल दिया है।",
    },
    "MEDICAL": {
        "en-IN": "I am the clinic's AI receptionist, so I cannot give medical advice. I have asked our clinic staff to call you back. If it is urgent, please call 108 or 112.",
        "hi-IN": "मैं क्लिनिक की AI रिसेप्शनिस्ट हूँ, इसलिए दवा या इलाज की सलाह नहीं दे सकती। मैंने स्टाफ को आपको कॉल करने के लिए कहा है। ज़रूरी हो तो 108 या 112 पर कॉल करें।",
        "hi-en": "मैं clinic की AI receptionist हूँ, इसलिए medicine या इलाज की advice नहीं दे सकती। मैंने staff को आपको call back करने के लिए बोल दिया है। Urgent हो तो 108 या 112 पर call करें।",
    },
    "HUMAN": {
        "en-IN": "Sure. I have asked our reception staff to call you back during clinic hours.",
        "hi-IN": "ज़रूर। मैंने रिसेप्शन स्टाफ को क्लिनिक समय में आपको कॉल करने के लिए कह दिया है।",
        "hi-en": "ज़रूर। मैंने reception staff को clinic timing में आपको call back करने के लिए बोल दिया है।",
    },
    "REPEAT": {
        "en-IN": "Sorry, I could not hear that clearly. Could you please say it again?",
        "hi-IN": "माफ़ कीजिए, मैं ठीक से सुन नहीं पाई। क्या आप दोबारा बोल सकते हैं?",
        "hi-en": "Sorry, मैं ठीक से सुन नहीं पाई। Please एक बार फिर से बोलिए।",
    },
    "NOT_UNDERSTOOD": {
        "en-IN": "I am having trouble understanding. I have asked our reception staff to call you back.",
        "hi-IN": "मुझे समझने में परेशानी हो रही है। मैंने रिसेप्शन स्टाफ को आपको कॉल करने के लिए कह दिया है।",
        "hi-en": "मुझे समझने में थोड़ी problem हो रही है। मैंने reception staff को आपको call back करने के लिए बोल दिया है।",
    },
    "SYSTEM_ERROR": {
        "en-IN": "Sorry, I am facing a technical problem. I have asked our staff to call you back.",
        "hi-IN": "माफ़ कीजिए, अभी तकनीकी समस्या है। मैंने स्टाफ को आपको कॉल करने के लिए कह दिया है।",
        "hi-en": "Sorry, अभी technical problem है। मैंने staff को आपको call back करने के लिए बोल दिया है।",
    },
    "GREETING": {
        "en-IN": "Hello, welcome to Mahanadi Aarogya Clinic. I am the clinic's AI receptionist. How can I help you?",
        "hi-IN": "नमस्ते, महानदी आरोग्य क्लिनिक में आपका स्वागत है। मैं क्लिनिक की AI रिसेप्शनिस्ट हूँ। बताइए, मैं आपकी क्या मदद करूँ?",
        "hi-en": "नमस्ते, Mahanadi Aarogya Clinic में आपका स्वागत है। मैं clinic की AI receptionist हूँ। आप Hindi या English में बोल सकते हैं। बताइए, मैं क्या help करूँ?",
    },
}


def reply(kind: str, lang: str) -> str:
    return REPLIES[kind].get(lang, REPLIES[kind]["hi-en"])


# ---------------------------------------------------------------- language guess

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_HINGLISH_MARKERS = re.compile(
    r"\b(hai|hain|kya|mujhe|mujhko|chahiye|kal|aaj|parso|ka|ki|ke|nahi|nahin|haan|ha|aap|mera|meri|"
    r"karna|karni|kar|kijiye|dijiye|batao|bataiye|wala|wali|subah|shaam|baje|ji|theek|accha|acha|"
    r"main|hum|kab|kitna|kitni|doctor sahab)\b", re.I)


def guess_lang(text: str, fallback: str = "hi-en") -> str:
    """hi-IN = Devanagari only; hi-en = Hindi with English words, or Roman Hindi; en-IN = English."""
    if not text.strip():
        return fallback
    has_dev = bool(_DEVANAGARI.search(text))
    latin = _LATIN_WORD.findall(text)
    if has_dev:
        return "hi-en" if latin else "hi-IN"
    if latin and len(_HINGLISH_MARKERS.findall(text)) >= max(1, len(latin) // 6):
        return "hi-en"
    return "en-IN" if latin else fallback


# ---------------------------------------------------------------- privacy scrub

_SCRUB = [
    (re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"), "[phone removed]"),
    (re.compile(r"(?<!\d)\d{4}[\s-]?\d{4}[\s-]?\d{4}(?!\d)"), "[id removed]"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.I), "[id removed]"),
]


def scrub(text: str) -> str:
    """Remove phone, Aadhaar-like and PAN-like numbers before anything is stored."""
    for pattern, repl in _SCRUB:
        text = pattern.sub(repl, text)
    return text
