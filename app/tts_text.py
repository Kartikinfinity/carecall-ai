"""Turn digits, times, money, dates and booking references into spoken words.

Gnani's TTS docs: "Pass numbers, dates, and currency as spoken words to avoid
mispronunciations." The LLM is also told to write words, but this runs on
every reply before Timbre so a stray "₹400" or "10:30" is never misread.

lang: "en-IN" -> English words; "hi-IN" and "hi-en" -> Hindi (Devanagari)
number words, which is what the chosen Poorvi/Nalini voices read best.
Booking references are read digit by digit.
"""
from __future__ import annotations

import re
from datetime import date

HI_0_99 = (
    "शून्य एक दो तीन चार पाँच छह सात आठ नौ दस ग्यारह बारह तेरह चौदह पंद्रह सोलह सत्रह अठारह उन्नीस "
    "बीस इक्कीस बाईस तेईस चौबीस पच्चीस छब्बीस सत्ताईस अट्ठाईस उनतीस तीस इकतीस बत्तीस तैंतीस चौंतीस "
    "पैंतीस छत्तीस सैंतीस अड़तीस उनतालीस चालीस इकतालीस बयालीस तैंतालीस चवालीस पैंतालीस छियालीस "
    "सैंतालीस अड़तालीस उनचास पचास इक्यावन बावन तिरपन चौवन पचपन छप्पन सत्तावन अट्ठावन उनसठ साठ "
    "इकसठ बासठ तिरसठ चौंसठ पैंसठ छियासठ सड़सठ अड़सठ उनहत्तर सत्तर इकहत्तर बहत्तर तिहत्तर चौहत्तर "
    "पचहत्तर छिहत्तर सतहत्तर अठहत्तर उन्यासी अस्सी इक्यासी बयासी तिरासी चौरासी पचासी छियासी सत्तासी "
    "अट्ठासी नवासी नब्बे इक्यानवे बानवे तिरानवे चौरानवे पचानवे छियानवे सत्तानवे अट्ठानवे निन्यानवे"
).split()
assert len(HI_0_99) == 100

EN_ONES = ("zero one two three four five six seven eight nine ten eleven twelve thirteen "
           "fourteen fifteen sixteen seventeen eighteen nineteen").split()
EN_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()

EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
             "September", "October", "November", "December"]
HI_MONTHS = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त",
             "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]
EN_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HI_DAYS = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]
EN_ORDINAL = {1: "first", 2: "second", 3: "third", 5: "fifth", 8: "eighth", 9: "ninth", 12: "twelfth"}


# ------------------------------------------------------------------ numbers

def en_number(n: int) -> str:
    if n < 0:
        return "minus " + en_number(-n)
    if n < 20:
        return EN_ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return EN_TENS[tens] + ("" if ones == 0 else " " + EN_ONES[ones])
    for size, word in ((10_000_000, "crore"), (100_000, "lakh"), (1000, "thousand"), (100, "hundred")):
        if n >= size:
            head, rest = divmod(n, size)
            return f"{en_number(head)} {word}" + ("" if rest == 0 else " " + en_number(rest))
    raise AssertionError(n)


def hi_number(n: int) -> str:
    if n < 0:
        return "माइनस " + hi_number(-n)
    if n < 100:
        return HI_0_99[n]
    for size, word in ((10_000_000, "करोड़"), (100_000, "लाख"), (1000, "हज़ार"), (100, "सौ")):
        if n >= size:
            head, rest = divmod(n, size)
            return f"{hi_number(head)} {word}" + ("" if rest == 0 else " " + hi_number(rest))
    raise AssertionError(n)


def en_ordinal(n: int) -> str:
    if n in EN_ORDINAL:
        return EN_ORDINAL[n]
    if n > 20 and n % 10 and n < 100:
        return EN_TENS[n // 10] + " " + en_ordinal(n % 10)
    word = en_number(n)
    return word[:-1] + "ieth" if word.endswith("y") else word + "th"


def digits(s: str, lang: str) -> str:
    words = EN_ONES if lang == "en-IN" else HI_0_99
    return " ".join(words[int(c)] for c in s)


# ------------------------------------------------------------------ times

def _hour_12(h: int) -> int:
    return 12 if h % 12 == 0 else h % 12


def en_time(h: int, m: int, suffix: str | None) -> str:
    if suffix:
        ampm = suffix.replace(".", "").upper()
        if ampm == "PM" and h < 12:
            h += 12
        if ampm == "AM" and h == 12:
            h = 0
    period = "A M" if h < 12 else "P M"
    hour = en_number(_hour_12(h))
    if m == 0:
        return f"{hour} {period}"
    minute = f"oh {en_number(m)}" if m < 10 else en_number(m)
    return f"{hour} {minute} {period}"


def hi_time(h: int, m: int, suffix: str | None) -> str:
    if suffix:
        ampm = suffix.replace(".", "").upper()
        if ampm == "PM" and h < 12:
            h += 12
        if ampm == "AM" and h == 12:
            h = 0
    period = ("सुबह" if 4 <= h < 12 else "दोपहर" if 12 <= h < 16
              else "शाम" if 16 <= h < 20 else "रात")
    h12 = _hour_12(h)
    nxt = _hour_12(h + 1)
    if m == 0:
        spoken = f"{hi_number(h12)} बजे"
    elif m == 30:
        spoken = {1: "डेढ़ बजे", 2: "ढाई बजे"}.get(h12, f"साढ़े {hi_number(h12)} बजे")
    elif m == 15:
        spoken = f"सवा {hi_number(h12)} बजे"
    elif m == 45:
        spoken = f"पौने {hi_number(nxt)} बजे"
    else:
        spoken = f"{hi_number(h12)} बजकर {hi_number(m)} मिनट"
    return f"{period} {spoken}"


# ------------------------------------------------------------------ patterns

REF = re.compile(r"\bCC\s*-\s*(\d{3,6})\b", re.I)
ISO_DATE = re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b")
DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(EN_MONTHS + [m[:3] for m in EN_MONTHS])
                       + r"|" + "|".join(HI_MONTHS) + r")\b", re.I)
# "10:30", "10:30 AM", "5 PM", "5:00 p.m."; the optional suffix keeps "10:30 बजे" Hindi-friendly.
_AMPM = r"[AaPp](?:\.\s?[Mm]\.|[Mm]\b)"          # "AM", "pm", "a.m.", "p. m."
TIME = re.compile(rf"\b(\d{{1,2}}):(\d{{2}})(?:\s*({_AMPM}))?(?!\d)|\b(\d{{1,2}})\s*({_AMPM})")
MONEY = re.compile(r"(?:₹|\bRs\.?\s*|\bINR\s*)\s*(\d[\d,]*)|\b(\d[\d,]*)\s*(?:rupees|rupaye|rupaiye|रुपये|रुपए|रु\.?)",
                   re.I)
NUMBER = re.compile(r"\d[\d,]*")
SPACES = re.compile(r"[ \t]{2,}")


_PERIOD_WORD = re.compile(r"(सुबह|दोपहर|शाम|रात|subah|subha|dopahar|shaam|sham|raat|"
                          r"morning|afternoon|evening|night)", re.I)
_PM_PERIODS = {"शाम", "रात", "shaam", "sham", "raat", "evening", "night"}
_NOON_PERIODS = {"दोपहर", "dopahar", "afternoon"}


def _infer_24h(h: int, before: str) -> int:
    """Read a 12-hour time without AM/PM the way a person would.

    The LLM sometimes writes "शाम 05:00" or "09:00 से 01:30". A period word
    earlier in the same sentence decides; otherwise 1-6 o'clock means
    afternoon, because the clinic is never open at 1-6 AM.
    """
    sentence = re.split(r"[।.?!]", before)[-1]
    periods = _PERIOD_WORD.findall(sentence)
    period = periods[-1].lower() if periods else ""
    if 1 <= h <= 6:
        return h + 12
    if period in _PM_PERIODS or (period in _NOON_PERIODS and h <= 5):
        return h + 12
    return h


def _month_index(name: str) -> int:
    for i, (en, hi) in enumerate(zip(EN_MONTHS, HI_MONTHS)):
        if name.lower() in (en.lower(), en[:3].lower()) or name == hi:
            return i
    raise ValueError(name)


def normalize(text: str, lang: str = "hi-en") -> str:
    en = lang == "en-IN"
    num = en_number if en else hi_number

    text = re.sub(r"\bDr\.?(?=\s)", "Doctor", text)
    text = text.replace("डॉ.", "डॉक्टर")

    def ref(m):
        letters = "C C" if en or lang == "hi-en" else "सी सी"
        return f"{letters} {digits(m.group(1), 'en-IN' if lang == 'hi-en' else lang)}"
    text = REF.sub(ref, text)

    def iso(m):
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if en:
            return f"{EN_DAYS[d.weekday()]}, {en_ordinal(d.day)} {EN_MONTHS[d.month - 1]}"
        return f"{HI_DAYS[d.weekday()]}, {hi_number(d.day)} {HI_MONTHS[d.month - 1]}"
    text = ISO_DATE.sub(iso, text)

    def day_month(m):
        i = _month_index(m.group(2))
        day = int(m.group(1))
        return f"{en_ordinal(day)} {EN_MONTHS[i]}" if en else f"{hi_number(day)} {HI_MONTHS[i]}"
    text = DAY_MONTH.sub(day_month, text)

    def clock(m):
        if m.group(1) is not None:
            h, mi, suffix = int(m.group(1)), int(m.group(2)), m.group(3)
        else:
            h, mi, suffix = int(m.group(4)), 0, m.group(5)
        if h > 23 or mi > 59:
            return m.group(0)
        if not suffix and h < 12:
            h = _infer_24h(h, m.string[:m.start()])
        return en_time(h, mi, suffix) if en else hi_time(h, mi, suffix)
    text = TIME.sub(clock, text)
    if not en:
        # "सुबह सुबह साढ़े दस बजे बजे" -> drop the doubled words a Hindi sentence already had.
        text = re.sub(r"(?:सुबह|दोपहर|शाम|रात|subah|subha|dopahar|shaam|sham|raat)\s+(सुबह|दोपहर|शाम|रात)(?=\s)",
                      r"\1", text, flags=re.I)
        text = re.sub(r"(बजे|मिनट)\s+बजे", r"\1", text)

    def money(m):
        amount = int((m.group(1) or m.group(2)).replace(",", ""))
        return f"{num(amount)} rupees" if en else f"{num(amount)} रुपये"
    text = MONEY.sub(money, text)

    text = NUMBER.sub(lambda m: num(int(m.group(0).replace(",", ""))), text)
    return SPACES.sub(" ", text).strip()
