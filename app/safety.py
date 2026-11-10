"""Deterministic safety and privacy rules that never depend on the LLM.

- EMERGENCY: red-flag symptoms -> stop, tell the caller to call 108/112, urgent ticket.
- MEDICAL:   requests for medicine, dosage, diagnosis or report interpretation -> refuse, urgent ticket.
- HUMAN:     the caller asks for a person -> callback ticket.
A plain symptom mentioned while booking ("bukhar hai, doctor ko dikhana hai")
is *not* escalated: booking a visit for a fever is exactly the clinic's job.

The Hindi, Hinglish and English patterns come from testing with real spoken
sentences. The patterns for the other eight languages (REGIONAL) cover the same
ideas in each script; they err on the side of escalating.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from . import languages, phrases

EMERGENCY = [
    # Up to three words may sit in between ("seene mein bahut tez dard"), as people actually speak.
    r"chest pain", r"pain in (my )?chest", r"(seena|seene|sine|sina) (me|mein|mai|men)n?(\s+\S+){0,3}\s+(dard|pain|bhaari|bharipan)",
    r"सीने में(\s+\S+){0,3}\s+(दर्द|भारीपन)", r"छाती में(\s+\S+){0,3}\s+दर्द",
    r"(chhati|chaati|chati) (me|mein)(\s+\S+){0,3}\s+dard", r"heart attack", r"हार्ट अटैक", r"dil ka daura",
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
    r"\bhuman\b", r"real person", r"talk to (a |some |the |the clinic )?\s*(person|someone|staff|receptionist)",
    r"speak (to|with) (a|the|some)?\s*(person|someone|staff|receptionist|human)", r"\bagent\b",
    r"insaa?n se", r"इंसान से", r"aadmi se baat", r"kisi se baat", r"staff se baat", r"स्टाफ से बात",
    r"receptionist se baat", r"रिसेप्शन(िस्ट)? से बात", r"reception se baat", r"call back (me|kar)", r"callback",
]

# Chest pain, breathing trouble, fainting, heart attack, heavy bleeding, stroke,
# seizure, poison, self-harm and accidents; then "which medicine / what dose";
# then "let me talk to a person" - in each language's own script.
_GAP = r"(\s+\S+){0,3}"
REGIONAL = {
    "mr-IN": {
        "EMERGENCY": [rf"छातीत{_GAP}\s+(दुख|वेदना|कळ)", r"श्वास (घेता|घ्यायला|लागत)", r"बेशुद्ध", r"हृदयविकार",
                      r"रक्तस्त्राव", r"रक्तस्राव", r"विष (घेतल|प्याय)", r"अपघात", r"झटका आला"],
        "MEDICAL": [r"(कोणते|कोणती|कोणतं) (औषध|गोळी)", r"औषध (सांगा|द्या|घेऊ)", r"किती (गोळ्या|डोस)", r"डोस"],
        "HUMAN": [r"(स्टाफशी|कर्मचाऱ्यांशी|माणसाशी|रिसेप्शनिस्टशी) बोल"],
    },
    "bn-IN": {
        "EMERGENCY": [rf"বুকে{_GAP}\s+(ব্যথা|ব্যাথা|যন্ত্রণা)", r"বুকের ব্যথা", r"শ্বাস (নিতে|কষ্ট|বন্ধ)", r"শ্বাসকষ্ট",
                      r"অজ্ঞান", r"হার্ট অ্যাটাক", r"রক্তপাত", r"রক্ত (পড়|বন্ধ হচ্ছে না|বের)", r"স্ট্রোক", r"খিঁচুনি",
                      r"বিষ খে", r"আত্মহত্যা", r"দুর্ঘটনা", r"অ্যাক্সিডেন্ট"],
        "MEDICAL": [r"(কোন|কী|কি) (ওষুধ|ঔষধ)", r"(ওষুধ|ঔষধ) (বলুন|দিন|খাব|খাবো)", r"ডোজ", r"কয়টা (ওষুধ|ট্যাবলেট)"],
        "HUMAN": [r"(স্টাফের|কর্মীর|মানুষের|কারও|রিসেপশনিস্টের) (সঙ্গে|সাথে) কথা"],
    },
    "gu-IN": {
        "EMERGENCY": [rf"છાતીમાં{_GAP}\s+(દુખાવો|દુઃખાવો|દુખે|દર્દ)", r"શ્વાસ (લેવામાં|નથી|ચડ)", r"બેભાન", r"હાર્ટ એટેક",
                      r"લોહી (વહ|નીકળ|બંધ)", r"રક્તસ્ત્રાવ", r"લકવો", r"ખેંચ આવ", r"ઝેર", r"આત્મહત્યા", r"અકસ્માત"],
        "MEDICAL": [r"(કઈ|કયી) (દવા|ગોળી)", r"દવા (કહો|આપો|લઉં|બતાવો)", r"ડોઝ", r"કેટલી (ગોળી|દવા)"],
        "HUMAN": [r"(સ્ટાફ|માણસ|કોઈ|રિસેપ્શનિસ્ટ) સાથે વાત"],
    },
    "pa-IN": {
        "EMERGENCY": [rf"ਛਾਤੀ (ਵਿੱਚ|ਵਿਚ|ਚ){_GAP}\s+(ਦਰਦ|ਪੀੜ)", r"ਸਾਹ (ਨਹੀਂ|ਲੈਣ|ਚੜ੍ਹ)", r"ਬੇਹੋਸ਼", r"ਹਾਰਟ ਅਟੈਕ",
                      r"ਦਿਲ ਦਾ ਦੌਰਾ", r"ਖ਼?ੂਨ (ਵਗ|ਨਹੀਂ ਰੁਕ|ਬਹੁਤ)", r"ਲਕਵਾ", r"ਦੌਰਾ ਪ", r"ਜ਼?ਹਿਰ", r"ਖੁਦਕੁਸ਼ੀ",
                      r"ਹਾਦਸਾ", r"ਐਕਸੀਡੈਂਟ"],
        "MEDICAL": [r"(ਕਿਹੜੀ|ਕੇਹੜੀ) (ਦਵਾਈ|ਗੋਲੀ)", r"ਦਵਾਈ (ਦੱਸੋ|ਦਿਓ|ਲਵਾਂ|ਖਾਵਾਂ)", r"ਡੋਜ਼", r"ਖੁਰਾਕ",
                    r"ਕਿੰਨੀ (ਦਵਾਈ|ਗੋਲੀਆਂ)"],
        "HUMAN": [r"(ਸਟਾਫ਼?|ਬੰਦੇ|ਕਿਸੇ|ਰਿਸੈਪਸ਼ਨਿਸਟ) ਨਾਲ ਗੱਲ"],
    },
    "ta-IN": {
        "EMERGENCY": [rf"(நெஞ்சு|நெஞ்சில்|மார்பு|மார்பில்){_GAP}\s*வலி", r"மூச்சு (விட|திணற|வாங்க)", r"மூச்சுத் ?திணறல்",
                      r"மயக்கம்", r"மயங்கி", r"மாரடைப்பு", r"ஹார்ட் அட்டாக்", r"இரத்தப்போக்கு", r"ரத்தப்போக்கு",
                      r"பக்கவாதம்", r"வலிப்பு", r"விஷம்", r"தற்கொலை", r"விபத்து"],
        "MEDICAL": [r"(எந்த|என்ன) (மருந்து|மாத்திரை)", r"(மருந்து|மாத்திரை) (சொல்லுங்க|கொடுங்க|எடுக்க|சாப்பிட)",
                    r"டோஸ்", r"எத்தனை மாத்திரை"],
        "HUMAN": [r"(ஊழியரிடம்|ஊழியர்களிடம்|ஒருவரிடம்|மனிதரிடம்|ஸ்டாஃபிடம்) பேச"],
    },
    "te-IN": {
        "EMERGENCY": [rf"(ఛాతీ|ఛాతి|గుండె)(లో)?{_GAP}\s*నొప్పి", r"(ఊపిరి|శ్వాస) (ఆడ|తీసుకో|అంద)", r"స్పృహ (తప్ప|కోల్పో|లేదు)",
                      r"గుండెపోటు", r"హార్ట్ ?ఎటాక్", r"రక్తస్రావం", r"పక్షవాతం", r"మూర్ఛ", r"ఫిట్స్", r"విషం",
                      r"ఆత్మహత్య", r"ప్రమాదం", r"యాక్సిడెంట్"],
        "MEDICAL": [r"\sఏ (మందు|మందులు|టాబ్లెట్)", r"(మందు|మందులు) (చెప్పండి|ఇవ్వండి|వేసుకో|తీసుకో)", r"డోస్",
                    r"మోతాదు", r"ఎన్ని (మాత్రలు|టాబ్లెట్లు)"],
        "HUMAN": [r"(సిబ్బందితో|మనిషితో|ఎవరితోనైనా|స్టాఫ్తో) మాట్లాడ"],
    },
    "kn-IN": {
        "EMERGENCY": [rf"ಎದೆ(ಯಲ್ಲಿ)?{_GAP}\s*ನೋವು", r"ಉಸಿರಾಟ(ದ)? (ತೊಂದರೆ|ಕಷ್ಟ)", r"ಉಸಿರಾಡಲು", r"ಉಸಿರು (ಕಟ್ಟ|ಬರುತ್ತಿಲ್ಲ)",
                      r"ಪ್ರಜ್ಞೆ (ತಪ್ಪ|ಇಲ್ಲ)", r"ಹೃದಯಾಘಾತ", r"ಹಾರ್ಟ್ ?ಅಟ್ಯಾಕ್", r"ರಕ್ತಸ್ರಾವ", r"ಪಾರ್ಶ್ವವಾಯು", r"ಮೂರ್ಛೆ",
                      r"ಫಿಟ್ಸ್", r"ವಿಷ (ಕುಡಿ|ಸೇವಿ|ತಿಂ)", r"ಆತ್ಮಹತ್ಯೆ", r"ಅಪಘಾತ"],
        "MEDICAL": [r"ಯಾವ (ಔಷಧಿ|ಔಷಧ|ಮಾತ್ರೆ)", r"(ಔಷಧಿ|ಮಾತ್ರೆ) (ಹೇಳಿ|ಕೊಡಿ|ತೆಗೆದುಕೊಳ್ಳ)", r"ಡೋಸ್", r"ಎಷ್ಟು ಮಾತ್ರೆ"],
        "HUMAN": [r"(ಸಿಬ್ಬಂದಿಯೊಂದಿಗೆ|ಸಿಬ್ಬಂದಿ ಜೊತೆ|ಮನುಷ್ಯರ ಜೊತೆ|ಯಾರಾದರೂ ಜೊತೆ|ಸ್ಟಾಫ್ ಜೊತೆ) ಮಾತ"],
    },
    "ml-IN": {
        "EMERGENCY": [rf"നെഞ്ച(ു|ിൽ|ിന്)?{_GAP}\s*വേദന", r"ശ്വാസം ?(മുട്ട|കിട്ട|എടുക്കാൻ)", r"ശ്വാസതടസ്സം",
                      r"ബോധം ?(കെട്ട|പോയി|ഇല്ല)", r"ഹൃദയാഘാതം", r"ഹാർട്ട് ?അറ്റാക്ക്", r"രക്തസ്രാവം", r"പക്ഷാഘാതം",
                      r"അപസ്മാരം", r"വിഷം", r"ആത്മഹത്യ", r"അപകടം"],
        "MEDICAL": [r"(ഏത്|എന്ത്) (മരുന്ന്|ഗുളിക)", r"(മരുന്ന്|ഗുളിക) (പറയ|തര|കഴിക്ക)", r"ഡോസ്", r"എത്ര ഗുളിക"],
        "HUMAN": [r"(ജീവനക്കാരോട്|സ്റ്റാഫിനോട്|ആരോടെങ്കിലും|മനുഷ്യനോട്) സംസാരിക്ക"],
    },
}


def _clean(text: str) -> str:
    """One spelling per word: composed letters, no zero-width joiners."""
    return languages.strip_joiners(unicodedata.normalize("NFC", text))


def _compile(base: list[str], kind: str) -> list[re.Pattern]:
    extra = [p for lang in REGIONAL.values() for p in lang[kind]]
    return [re.compile(_clean(p), re.I) for p in base + extra]


_EMERGENCY = _compile(EMERGENCY, "EMERGENCY")
_MEDICAL = _compile(MEDICAL_ADVICE, "MEDICAL")
_HUMAN = _compile(HUMAN, "HUMAN")


@dataclass(frozen=True)
class SafetyResult:
    kind: str | None          # "EMERGENCY" | "MEDICAL" | "HUMAN" | None
    matched: str = ""


def _prep(text: str) -> str:
    return " " + _clean(text).lower().replace("’", "'") + " "


def check(text: str) -> SafetyResult:
    t = _prep(text)
    for kind, patterns in (("EMERGENCY", _EMERGENCY), ("MEDICAL", _MEDICAL), ("HUMAN", _HUMAN)):
        for p in patterns:
            m = p.search(t)
            if m:
                return SafetyResult(kind, m.group(0).strip())
    return SafetyResult(None)


# ---------------------------------------------------------------- replies (no LLM involved)

REPLIES = phrases.REPLIES
reply = phrases.reply


# ---------------------------------------------------------------- language guess

_LATIN_WORD = re.compile(r"[A-Za-z]{2,}")
_HINGLISH_MARKERS = re.compile(
    r"\b(hai|hain|kya|mujhe|mujhko|chahiye|kal|aaj|parso|ka|ki|ke|nahi|nahin|haan|ha|aap|mera|meri|"
    r"karna|karni|kar|kijiye|dijiye|batao|bataiye|wala|wali|subah|shaam|baje|ji|theek|accha|acha|"
    r"main|hum|kab|kitna|kitni|doctor sahab)\b", re.I)
# Hindi and Marathi share Devanagari, so only everyday words can tell them apart.
_MARATHI_MARKERS = re.compile(r"(?<![ऀ-ॿ])(आहे|आहेत|मला|पाहिजे|आणि|उद्या|तुम्ही|माझी|माझे|माझा|हवी|हवे|करायची|करायचे|नको|होय)(?![ऀ-ॿ])")
_HINDI_MARKERS = re.compile(r"(?<![ऀ-ॿ])(है|हैं|मुझे|चाहिए|और|कल|आप|मेरा|मेरी|नहीं|हाँ|हां|कर दीजिए|बजे)(?![ऀ-ॿ])")
_SWITCH_TO_ENGLISH_WORDS = 4


def guess_lang(text: str, fallback: str = "hi-en") -> str:
    """Work out the reply language from what was written.

    `fallback` is the language the call is in right now. A script that belongs to
    one language settles it. Devanagari is Hindi/Hinglish unless the words (or the
    call so far) say Marathi. Latin text is English or Roman Hindi; in a call that
    is running in another language, a stray English word or two changes nothing.
    """
    if not text.strip():
        return fallback
    by_script = languages.from_script(text)
    if by_script:
        return by_script
    latin = _LATIN_WORD.findall(text)
    if languages.has_devanagari(text):
        marathi, hindi = len(_MARATHI_MARKERS.findall(text)), len(_HINDI_MARKERS.findall(text))
        if languages.is_supported("mr-IN") and (marathi > hindi or (fallback == "mr-IN" and marathi >= hindi)):
            return "mr-IN"
        return "hi-en" if latin else "hi-IN"
    if not latin:
        return fallback
    if fallback not in languages.CORE and len(latin) < _SWITCH_TO_ENGLISH_WORDS:
        return fallback
    if len(_HINGLISH_MARKERS.findall(text)) >= max(1, len(latin) // 6):
        return "hi-en"
    return "en-IN"


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
