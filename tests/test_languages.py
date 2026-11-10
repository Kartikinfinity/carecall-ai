"""Every supported language must be complete: voice, fixed phrases, safety words, buttons.

These tests run over the language table, so a language added to app/languages.py
without its phrases or safety patterns fails here instead of in front of a caller.
"""
import httpx
import pytest

from app import languages, phrases, safety
from app.dialog import is_no, is_yes
from app.gnani import GnaniClient, tone_wav
from app.safety import check, guess_lang
from app.tts_text import normalize

from .test_api import client  # noqa: F401  (fixture)
from .test_gnani import make_client, run

CODES = languages.codes()
REGIONAL = [c for c in CODES if c not in languages.CORE]
GNANI_TEN = {"hi-IN", "en-IN", "bn-IN", "gu-IN", "kn-IN", "ml-IN", "mr-IN", "pa-IN", "ta-IN", "te-IN"}

# (chest pain with a word in between, a medicine question, an ordinary booking request)
SAMPLES = {
    "mr-IN": ("माझ्या छातीत खूप दुखत आहे", "कोणती गोळी घेऊ?", "मला उद्या डॉक्टर चंद्राकर यांची अपॉइंटमेंट हवी आहे"),
    "bn-IN": ("আমার বুকে খুব ব্যথা হচ্ছে", "কোন ওষুধ খাব?", "আমি কাল ডাক্তার চন্দ্রাকরের অ্যাপয়েন্টমেন্ট চাই"),
    "gu-IN": ("મારી છાતીમાં બહુ દુખાવો થાય છે", "કઈ દવા લઉં?", "મારે કાલે ડૉક્ટર ચંદ્રાકરની એપોઇન્ટમેન્ટ જોઈએ છે"),
    "pa-IN": ("ਮੇਰੀ ਛਾਤੀ ਵਿੱਚ ਬਹੁਤ ਦਰਦ ਹੋ ਰਿਹਾ ਹੈ", "ਕਿਹੜੀ ਦਵਾਈ ਲਵਾਂ?", "ਮੈਨੂੰ ਕੱਲ੍ਹ ਡਾਕਟਰ ਚੰਦਰਾਕਰ ਦੀ ਅਪਾਇੰਟਮੈਂਟ ਚਾਹੀਦੀ ਹੈ"),
    "ta-IN": ("எனக்கு நெஞ்சு ரொம்ப வலிக்கிறது", "எந்த மாத்திரை சாப்பிடணும்?", "நாளைக்கு டாக்டர் சந்திராகர் அப்பாயிண்ட்மெண்ட் வேண்டும்"),
    "te-IN": ("నాకు ఛాతీలో చాలా నొప్పిగా ఉంది", "ఏ మందు వేసుకోవాలి?", "రేపు డాక్టర్ చంద్రాకర్ అపాయింట్‌మెంట్ కావాలి"),
    "kn-IN": ("ನನಗೆ ಎದೆಯಲ್ಲಿ ತುಂಬಾ ನೋವು ಇದೆ", "ಯಾವ ಮಾತ್ರೆ ತೆಗೆದುಕೊಳ್ಳಲಿ?", "ನಾಳೆ ಡಾಕ್ಟರ್ ಚಂದ್ರಾಕರ್ ಅಪಾಯಿಂಟ್‌ಮೆಂಟ್ ಬೇಕು"),
    "ml-IN": ("എനിക്ക് നെഞ്ചിൽ വല്ലാത്ത വേദന ഉണ്ട്", "ഏത് ഗുളിക കഴിക്കണം?", "നാളെ ഡോക്ടർ ചന്ദ്രാകറിന്റെ അപ്പോയിന്റ്മെന്റ് വേണം"),
}


# ------------------------------------------------------------------ the table itself

def test_table_covers_gnanis_ten_languages_plus_hinglish():
    assert len(CODES) == len(set(CODES)) == 11
    assert {languages.get(c).stt_code for c in CODES} == GNANI_TEN
    assert {languages.get(c).tts_code for c in CODES} == GNANI_TEN | {"hi-en"}
    assert set(SAMPLES) == set(REGIONAL)


def test_unknown_or_missing_code_falls_back_to_default():
    assert languages.get("xx-XX").code == languages.get(None).code == languages.DEFAULT
    assert not languages.is_supported("as-IN")


@pytest.mark.parametrize("code", CODES)
def test_each_language_has_a_voice_and_names(code):
    lang = languages.get(code)
    assert lang.voice and lang.name and lang.native and lang.html_lang
    assert GnaniClient().voice_for(code) == (lang.voice, lang.tts_code)


@pytest.mark.parametrize("code", CODES)
def test_prisma_codes_for_each_language(code):
    sent = languages.stt_codes(code).split(",")
    assert languages.get(code).stt_code in sent and "en-IN" in sent
    assert len(sent) <= languages.MAX_STT_CODES


def test_auto_detect_uses_indic_auto():
    assert languages.stt_codes("ta-IN", auto=True) == "indic-auto"
    assert languages.stt_codes("ta-IN") == "ta-IN,en-IN"
    assert languages.stt_codes("hi-en", core_default="hi-IN,en-IN") == "hi-IN,en-IN"


# ------------------------------------------------------------------ fixed phrases

@pytest.mark.parametrize("code", CODES)
@pytest.mark.parametrize("kind", phrases.KINDS)
def test_every_fixed_reply_exists_in_every_language(code, kind):
    assert phrases.REPLIES[kind].get(code, "").strip()


@pytest.mark.parametrize("code", CODES)
def test_emergency_and_medical_replies_give_the_numbers(code):
    for kind in ("EMERGENCY", "MEDICAL"):
        text = phrases.reply(kind, code)
        assert "108" in text and "112" in text


@pytest.mark.parametrize("code", CODES)
def test_done_message_and_buttons_are_complete(code):
    assert set(phrases.DONE[code]) >= {"book", "cancel", "reschedule", "other"}
    assert phrases.done({"action": "book", "ref": "CC-1042"}, code).endswith("Reference CC-1042.")
    assert "CC-" not in phrases.done({"action": "cancel", "ref": "CC-1042"}, code)
    buttons = phrases.buttons(code)
    assert set(buttons) == {"confirm", "decline", "staff", "slot"}
    assert "{date}" in buttons["slot"] and "{time}" in buttons["slot"]


@pytest.mark.parametrize("code", CODES)
def test_buttons_mean_what_they_say(code):
    buttons = phrases.buttons(code)
    assert is_yes(buttons["confirm"], code) and not is_no(buttons["confirm"], code)
    assert is_no(buttons["decline"], code) and not is_yes(buttons["decline"], code)
    assert check(buttons["staff"]).kind == "HUMAN"


def test_marathi_yes_is_not_a_hindi_yes():
    assert is_yes("हो", "mr-IN") and not is_yes("हो", "hi-IN")
    assert is_yes("ठीक है।", "hi-IN")          # a danda after the word must not hide it


# ------------------------------------------------------------------ language detection

@pytest.mark.parametrize("code", REGIONAL)
def test_reply_script_decides_the_language(code):
    assert guess_lang(phrases.reply("GREETING", code), fallback="hi-en") == code
    assert guess_lang(SAMPLES[code][2], fallback="hi-en") == code


def test_hindi_is_not_mistaken_for_marathi():
    hindi = "मुझे कल डॉक्टर चंद्राकर का अपॉइंटमेंट चाहिए"
    assert guess_lang(hindi, fallback="hi-IN") == "hi-IN"
    assert guess_lang(hindi, fallback="mr-IN") == "hi-IN"
    assert guess_lang("ठीक", fallback="mr-IN") == "mr-IN"      # no clue either way: stay in the call's language


def test_a_stray_english_word_does_not_switch_a_regional_call():
    assert guess_lang("ok confirm", fallback="ta-IN") == "ta-IN"
    assert guess_lang("Doctor Chandrakar", fallback="bn-IN") == "bn-IN"
    assert guess_lang("I would like to book an appointment for tomorrow", fallback="ta-IN") == "en-IN"


# ------------------------------------------------------------------ safety

@pytest.mark.parametrize("code", REGIONAL)
def test_each_language_has_all_three_safety_lists(code):
    assert all(safety.REGIONAL[code][kind] for kind in ("EMERGENCY", "MEDICAL", "HUMAN"))


@pytest.mark.parametrize("code", REGIONAL)
def test_chest_pain_is_an_emergency_in_every_language(code):
    assert check(SAMPLES[code][0]).kind == "EMERGENCY"


@pytest.mark.parametrize("code", REGIONAL)
def test_medicine_questions_are_refused_in_every_language(code):
    assert check(SAMPLES[code][1]).kind == "MEDICAL"


@pytest.mark.parametrize("code", REGIONAL)
def test_an_ordinary_booking_request_is_not_escalated(code):
    assert check(SAMPLES[code][2]).kind is None


def test_kannada_word_for_topic_is_not_poison():
    assert check("ಈ ವಿಷಯ ಹೇಳಿ").kind is None


# ------------------------------------------------------------------ speech formatting

def test_regional_replies_keep_digits_but_fix_times_and_references():
    assert normalize("CC-1042", "ta-IN") == "C C 1 0 4 2"
    assert normalize("13:30", "bn-IN") == "1:30 PM"
    assert normalize("10:30", "te-IN") == "10:30 AM"
    assert normalize("108 / 112", "kn-IN") == "1 0 8 / 1 1 2"
    assert "₹400" in normalize("₹400", "ml-IN")          # Timbre says the amount itself


# ------------------------------------------------------------------ Gnani requests and API

def test_transcribe_sends_the_calls_language_to_prisma(tmp_path):
    seen = {}

    def handler(request: httpx.Request):
        seen["body"] = request.content.decode("utf-8", "replace")
        return httpx.Response(200, json={"transcript": "வணக்கம்", "detected_language_code": "ta-IN"})

    res = run(make_client(handler, tmp_path).transcribe(tone_wav(1.0), language_code=languages.stt_codes("ta-IN")))
    assert "ta-IN,en-IN" in seen["body"] and res.language == "ta-IN"


def test_synthesize_uses_the_languages_voice(tmp_path):
    seen = {}

    def handler(request: httpx.Request):
        seen["json"] = __import__("json").loads(request.content)
        return httpx.Response(200, content=tone_wav(0.2))

    res = run(make_client(handler, tmp_path).synthesize("வணக்கம்", "ta-IN"))
    assert (seen["json"]["voice"], seen["json"]["language"]) == ("Trisha", "ta-IN") == (res.voice, res.language)


def test_languages_endpoint_lists_everything_the_browser_needs(client):  # noqa: F811
    body = client.get("/api/languages").json()
    assert body["default"] == "hi-en" and body["auto"] == "auto"
    assert [l["code"] for l in body["languages"]] == list(CODES)
    assert all(set(l["phrases"]) == {"confirm", "decline", "staff", "slot"} for l in body["languages"])


def test_a_call_can_start_in_any_language_or_auto(client):  # noqa: F811
    tamil = client.post("/api/session", json={"lang": "ta-IN", "speak": True}).json()
    assert tamil["lang"] == "ta-IN" and tamil["reply"] == phrases.reply("GREETING", "ta-IN")
    assert tamil["tts"]["voice"] == "Trisha" and tamil["auto"] is False
    auto = client.post("/api/session", json={"lang": "auto", "speak": False}).json()
    assert auto["auto"] is True and auto["lang"] == "hi-en"
    odd = client.post("/api/session", json={"lang": "xx-XX", "speak": False}).json()
    assert odd["lang"] == "hi-en"


def test_emergency_in_tamil_gets_the_tamil_fixed_reply(client):  # noqa: F811
    start = client.post("/api/session", json={"lang": "ta-IN", "speak": False}).json()
    turn = client.post("/api/turn", json={"session_id": start["session_id"], "text": SAMPLES["ta-IN"][0],
                                          "speak": False}).json()
    assert turn["source"] == "safety" and turn["card"]["emergency"] is True
    assert turn["lang"] == "ta-IN" and turn["reply"] == phrases.reply("EMERGENCY", "ta-IN")
