import pytest

from app.dialog import is_no, is_yes
from app.safety import check, guess_lang, scrub


@pytest.mark.parametrize("text", [
    "I have chest pain", "Mere seene mein dard ho raha hai", "सीने में दर्द है", "papa ko saans nahi aa rahi",
    "मेरी माँ बेहोश हो गई", "heavy bleeding after a fall", "I think it's a heart attack", "khoon nahi ruk raha",
])
def test_emergency(text):
    assert check(text).kind == "EMERGENCY"


@pytest.mark.parametrize("text", [
    "Which medicine should I take for fever?", "kaun si dawa lu?", "मुझे कौन सी दवा लेनी चाहिए",
    "What is the dose of paracetamol", "मेरी रिपोर्ट का मतलब क्या है", "BP ki dawa bata do", "how many tablets kitni goli",
])
def test_medical_advice(text):
    assert check(text).kind == "MEDICAL"


@pytest.mark.parametrize("text", [
    "I want to talk to a human", "kisi insaan se baat karao", "receptionist se baat karni hai",
    "मुझे स्टाफ से बात करनी है", "please speak to someone at the clinic",
])
def test_human(text):
    assert check(text).kind == "HUMAN"


@pytest.mark.parametrize("text", [
    "General medicine doctor ka appointment chahiye", "Mujhe bukhar hai, doctor ko dikhana hai",
    "Blood test ke liye fasting karna padega?", "Clinic kitne baje khulta hai?", "Book Dr. Baghel tomorrow",
    "मुझे कल सुबह का अपॉइंटमेंट चाहिए", "What is the fee for ultrasound?",
])
def test_ordinary_requests_are_not_escalated(text):
    assert check(text).kind is None


@pytest.mark.parametrize("text,lang", [
    ("What time does the clinic open?", "en-IN"), ("क्लिनिक कब खुलता है?", "hi-IN"),
    ("कल सुबह Dr. Chandrakar का appointment चाहिए", "hi-en"), ("Kal subah appointment chahiye", "hi-en"),
    ("Book Dr. Baghel tomorrow at 10", "en-IN"), ("", "hi-en"),
])
def test_guess_lang(text, lang):
    assert guess_lang(text) == lang


@pytest.mark.parametrize("text,yes,no", [
    ("haan", True, False), ("Haan, book kar do", True, False), ("yes please", True, False),
    ("जी हाँ, कर दीजिए", True, False), ("ठीक है", True, False), ("ji", True, False), ("confirm", True, False),
    ("nahi", False, True), ("No, wait", False, True), ("नहीं, शाम का चाहिए", False, True),
    ("koi problem nahi, book kar do", False, False), ("shaam ka slot hai kya?", False, False),
])
def test_yes_no(text, yes, no):
    assert is_yes(text) is yes and is_no(text) is no


def test_scrub_removes_ids_but_keeps_times_and_refs():
    out = scrub("Call me on 98765 43210 or +91-9876543210, Aadhaar 1234 5678 9012, PAN ABCDE1234F, CC-1042 at 10:30")
    assert "98765" not in out and "9876543210" not in out and "1234 5678" not in out and "ABCDE1234F" not in out
    assert "CC-1042" in out and "10:30" in out
