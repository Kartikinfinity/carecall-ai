import re

import pytest

from app.tts_text import en_number, hi_number, normalize


@pytest.mark.parametrize("n,words", [
    (0, "zero"), (7, "seven"), (15, "fifteen"), (40, "forty"), (45, "forty five"),
    (100, "one hundred"), (400, "four hundred"), (550, "five hundred fifty"),
    (1042, "one thousand forty two"), (150000, "one lakh fifty thousand"),
])
def test_en_number(n, words):
    assert en_number(n) == words


@pytest.mark.parametrize("n,words", [
    (5, "पाँच"), (19, "उन्नीस"), (45, "पैंतालीस"), (99, "निन्यानवे"), (400, "चार सौ"),
    (550, "पाँच सौ पचास"), (1200, "एक हज़ार दो सौ"), (250000, "दो लाख पचास हज़ार"),
])
def test_hi_number(n, words):
    assert hi_number(n) == words


@pytest.mark.parametrize("text,expected", [
    ("Dr. Chandrakar is free at 10:30 AM.", "Doctor Chandrakar is free at ten thirty A M."),
    ("Slot at 17:15", "Slot at five fifteen P M"),
    ("Come at 5 PM", "Come at five P M"),
    ("At 9:05 am", "At nine oh five A M"),
    ("The fee is ₹400.", "The fee is four hundred rupees."),
    ("Fee: Rs. 1,200", "Fee: one thousand two hundred rupees"),
    ("It costs 550 rupees", "It costs five hundred fifty rupees"),
    ("Your reference is CC-1042.", "Your reference is C C one zero four two."),
    ("On 2026-11-12 at 10:00", "On Thursday, twelfth November at ten A M"),
    ("on 21 November", "on twenty first November"),
])
def test_normalize_english(text, expected):
    assert normalize(text, "en-IN") == expected


@pytest.mark.parametrize("text,expected", [
    ("डॉक्टर कल 10:30 बजे उपलब्ध हैं।", "डॉक्टर कल सुबह साढ़े दस बजे उपलब्ध हैं।"),
    ("कल सुबह 10:30 बजे", "कल सुबह साढ़े दस बजे"),
    ("17:00 को", "शाम पाँच बजे को"),
    ("11:15 या 11:45", "सुबह सवा ग्यारह बजे या सुबह पौने बारह बजे"),
    ("1:30 PM", "दोपहर डेढ़ बजे"),
    ("2:30 PM", "दोपहर ढाई बजे"),
    ("10:20 पर", "सुबह दस बजकर बीस मिनट पर"),
    ("19:40", "शाम सात बजकर चालीस मिनट"),
    ("फ़ीस ₹600 है", "फ़ीस छह सौ रुपये है"),
    ("fee 500 रुपये है", "fee पाँच सौ रुपये है"),
    ("2026-11-12", "गुरुवार, बारह नवंबर"),
    ("12 November", "बारह नवंबर"),
])
def test_normalize_hindi(text, expected):
    assert normalize(text, "hi-IN") == expected


def test_normalize_hinglish_mixed_script_keeps_english_words():
    out = normalize("जी हाँ, Dr. Chandrakar कल 10:30 बजे available हैं। Fee ₹400 है। Reference CC-1042.",
                    "hi-en")
    assert out == ("जी हाँ, Doctor Chandrakar कल सुबह साढ़े दस बजे available हैं। Fee चार सौ रुपये है। "
                   "Reference C C one zero four two.")


@pytest.mark.parametrize("text,lang,expected", [
    # Real Flash-Lite output: 12-hour times written without AM/PM.
    ("क्लिनिक सुबह 09:00 से 01:30 और शाम 05:00 से 08:00 बजे तक खुलता है।", "hi-en",
     "क्लिनिक सुबह नौ बजे से दोपहर डेढ़ बजे और शाम पाँच बजे से रात आठ बजे तक खुलता है।"),
    ("Kal shaam 05:30 baje aaiye", "hi-en", "Kal शाम साढ़े पाँच बजे baje aaiye"),
    ("Open in the evening from 5:00 to 8:00", "en-IN", "Open in the evening from five P M to eight P M"),
    ("Morning slot at 9:45 or 10:00", "en-IN", "Morning slot at nine forty five A M or ten A M"),
    ("सुबह 10:00 बजे। शाम को नहीं", "hi-IN", "सुबह दस बजे। शाम को नहीं"),
])
def test_twelve_hour_times_read_in_context(text, lang, expected):
    assert normalize(text, lang) == expected


@pytest.mark.parametrize("lang", ["en-IN", "hi-IN", "hi-en"])
def test_no_digits_survive(lang):
    text = "CC-1043 on 2026-11-13 at 18:45, ₹350, call 108 or 112, 3 slots, 12 November"
    assert not re.search(r"\d", normalize(text, lang))


def test_text_without_numbers_unchanged():
    s = "Namaste, Mahanadi Aarogya Clinic mein aapka swagat hai."
    assert normalize(s, "hi-en") == s
