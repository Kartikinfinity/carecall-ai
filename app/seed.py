"""Synthetic demo data (adapted from research doc 5) and the seeder.

Everything here is fictional: the clinic, doctors, patients and contact IDs.
No dialable phone numbers are used anywhere; patients carry DEMO-IN-#### IDs.
Appointment and call dates are generated relative to "now" so the demo never
goes stale, whatever day it is run.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from .booking import BOOKING_LEAD, _dt, _iso, schedule_slots
from .config import now_ist
from .db import create_schema, transaction

MON_SAT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

CLINIC = {
    "id": "CLN-DEMO-01",
    "name_en": "Mahanadi Aarogya Polyclinic & Diagnostic Centre (fictional demo clinic)",
    "name_hi": "महानदी आरोग्य पॉलीक्लिनिक एवं डायग्नोस्टिक सेंटर (काल्पनिक डेमो क्लिनिक)",
    "address_en": "Plot 00-Fictional, Demo Enclave, Fictional Ring Link Road, Sector-X, Raipur, Chhattisgarh (not a real address)",
    "address_hi": "प्लॉट 00-काल्पनिक, डेमो एन्क्लेव, काल्पनिक रिंग लिंक रोड, सेक्टर-एक्स, रायपुर, छत्तीसगढ़ (वास्तविक पता नहीं)",
    "landmark": "Opposite Fictional Civic Park Gate No. 9",
    "hours_en": "Monday to Saturday, 9:00 AM to 1:30 PM and 5:00 PM to 8:00 PM. Blood sample collection from 8:00 AM. Closed on Sunday except the report counter, 9:00 AM to 12:00 PM.",
    "hours_hi": "सोमवार से शनिवार, सुबह 9:00 से दोपहर 1:30 और शाम 5:00 से रात 8:00 बजे तक। ब्लड सैंपल सुबह 8:00 बजे से। रविवार को बंद, केवल रिपोर्ट काउंटर सुबह 9:00 से 12:00 बजे तक खुला।",
    "payment_modes": "Cash, UPI and debit/credit cards at the reception counter",
}

DOCTORS = [
    {"id": "DOC-101", "name": "Dr. Aarav Chandrakar", "name_hi": "डॉ. आरव चंद्राकर",
     "aliases": ["Chandrakar", "Aarav Chandrakar", "Arav Chandrakar", "Chandra sir", "चंद्राकर",
                 "चांद्रकर", "चांद्रकार", "चंद्रकार", "चंद्रकर", "general physician", "physician",
                 "family doctor", "सामान्य डॉक्टर"],
     "specialty": "General & Family Medicine", "specialty_hi": "सामान्य चिकित्सा",
     "fee_inr": 400, "slot_mins": 15,
     "weekly": {d: [["09:30", "13:00"], ["17:00", "20:00"]] for d in MON_SAT},
     "breaks": [["11:00", "11:15"], ["18:30", "18:45"]]},
    {"id": "DOC-102", "name": "Dr. Meera Baghel", "name_hi": "डॉ. मीरा बघेल",
     "aliases": ["Baghel", "Meera Baghel", "Mira Baghel", "Meera Vaghela", "बघेल", "मीरा बघेल",
                 "pediatrician", "paediatrics", "pediatrics", "child specialist", "child doctor",
                 "bachchon ke", "बच्चों के", "बाल रोग"],
     "specialty": "Child Wellness & Pediatrics", "specialty_hi": "बाल स्वास्थ्य",
     "fee_inr": 500, "slot_mins": 20,
     "weekly": {d: [["10:00", "13:00"], ["17:00", "19:40"]] for d in MON_SAT},
     "breaks": [["11:20", "11:40"]]},
    {"id": "DOC-103", "name": "Dr. Rohan Deshmukh", "name_hi": "डॉ. रोहन देशमुख",
     "aliases": ["Deshmukh", "Rohan Deshmukh", "Rohan Deshmuk", "देशमुख", "रोहन देशमुख",
                 "orthopaedic", "orthopedic", "ortho", "bone doctor", "haddi", "haddi doctor",
                 "हड्डी", "हड्डी के", "joint pain"],
     "specialty": "Orthopaedics (Bone & Joint)", "specialty_hi": "हड्डी एवं जोड़ रोग",
     "fee_inr": 600, "slot_mins": 20,
     # Wednesday is Dr. Deshmukh's weekly off.
     "weekly": {d: [["10:00", "13:00"], ["17:30", "20:10"]] for d in ["Mon", "Tue", "Thu", "Fri", "Sat"]},
     "breaks": [["11:20", "11:40"]]},
    {"id": "DOC-104", "name": "Dr. Ananya Sahu", "name_hi": "डॉ. अनन्या साहू",
     "aliases": ["Sahu", "Ananya Sahu", "Ananya Sao", "Ananya Sahoo", "साहू", "अनन्या साहू",
                 "ultrasound", "sonography", "अल्ट्रासाउंड", "सोनोग्राफी", "ultrasound doctor"],
     "specialty": "Diagnostic Ultrasound", "specialty_hi": "अल्ट्रासाउंड जांच",
     "fee_inr": 800, "slot_mins": 30,
     "weekly": {d: [["09:00", "13:30"]] for d in MON_SAT},
     "breaks": [["11:00", "11:30"]]},
]

SERVICES = [
    {"id": "SRV-OPD-GEN", "name_en": "General consultation", "name_hi": "सामान्य परामर्श", "fee_inr": 400,
     "prep_en": "No special preparation. Bring previous prescriptions.",
     "prep_hi": "कोई विशेष तैयारी नहीं। पुरानी पर्चियाँ साथ लाएँ।", "walk_in": 0,
     "hours_en": "With Dr. Aarav Chandrakar, by appointment"},
    {"id": "SRV-OPD-PED", "name_en": "Child wellness consultation", "name_hi": "बाल स्वास्थ्य परामर्श", "fee_inr": 500,
     "prep_en": "A parent or guardian must accompany the child.",
     "prep_hi": "बच्चे के साथ माता-पिता या अभिभावक का होना ज़रूरी है।", "walk_in": 0,
     "hours_en": "With Dr. Meera Baghel, by appointment"},
    {"id": "SRV-OPD-ORT", "name_en": "Orthopaedic consultation", "name_hi": "हड्डी एवं जोड़ परामर्श", "fee_inr": 600,
     "prep_en": "Wear comfortable clothing and bring any earlier X-ray reports.",
     "prep_hi": "आरामदायक कपड़े पहनें और पुरानी एक्स-रे रिपोर्ट साथ लाएँ।", "walk_in": 0,
     "hours_en": "With Dr. Rohan Deshmukh, by appointment (off on Wednesday)"},
    {"id": "SRV-DGN-USG", "name_en": "Ultrasound scan", "name_hi": "अल्ट्रासाउंड जांच", "fee_inr": 800,
     "prep_en": "Arrive 20 minutes early with your doctor's requisition slip. Clinic staff will confirm any scan-specific preparation.",
     "prep_hi": "डॉक्टर की पर्ची के साथ 20 मिनट पहले आएँ। जांच से जुड़ी तैयारी क्लिनिक स्टाफ बताएगा।", "walk_in": 0,
     "hours_en": "With Dr. Ananya Sahu, mornings only, by appointment"},
    {"id": "SRV-DGN-BLD", "name_en": "Routine blood test (sample collection)", "name_hi": "रूटीन ब्लड टेस्ट (सैंपल)", "fee_inr": 550,
     "prep_en": "Walk in between 8:00 AM and 1:30 PM. Routine fasting panels usually need 8 to 10 hours of water-only fasting; if you take regular medicines or have a medical condition, confirm with clinic staff.",
     "prep_hi": "सुबह 8:00 से दोपहर 1:30 के बीच आएँ। फ़ास्टिंग जांच के लिए आमतौर पर 8 से 10 घंटे केवल पानी; नियमित दवा या कोई बीमारी हो तो स्टाफ से पुष्टि करें।", "walk_in": 1,
     "hours_en": "Walk-in, Monday to Saturday, 8:00 AM to 1:30 PM"},
    {"id": "SRV-DGN-ECG", "name_en": "X-ray and ECG", "name_hi": "एक्स-रे और ईसीजी", "fee_inr": 450,
     "prep_en": "Walk in during clinic hours. Reception will tell you about any test requirements.",
     "prep_hi": "क्लिनिक समय में आएँ। जांच की ज़रूरतें रिसेप्शन बताएगा।", "walk_in": 1,
     "hours_en": "Walk-in during clinic hours"},
]

# Policies only. Doctor timings and fees come from the doctors table, so an
# admin edit there can never contradict an FAQ answer.
FAQS = [
    ("FAQ-HOURS", "Timings", "timing hours open close khulta band samay time kab",
     "The clinic is open Monday to Saturday, 9:00 AM to 1:30 PM and 5:00 PM to 8:00 PM. Blood sample collection starts at 8:00 AM.",
     "क्लिनिक सोमवार से शनिवार सुबह 9:00 से दोपहर 1:30 और शाम 5:00 से रात 8:00 बजे तक खुला रहता है। ब्लड सैंपल सुबह 8:00 बजे से लिए जाते हैं।"),
    ("FAQ-SUNDAY", "Timings", "sunday ravivar holiday chutti closed",
     "On Sunday all consultations and scans are closed. Only the report collection counter is open, 9:00 AM to 12:00 PM.",
     "रविवार को सभी परामर्श और जांच बंद रहते हैं। केवल रिपोर्ट काउंटर सुबह 9:00 से 12:00 बजे तक खुला रहता है।"),
    ("FAQ-LOCATION", "Location", "address location where kahan pata landmark parking",
     "This is a fictional demo clinic at Plot 00-Fictional, Demo Enclave, Fictional Ring Link Road, Sector-X, Raipur, opposite Fictional Civic Park Gate No. 9. Free parking is available.",
     "यह एक काल्पनिक डेमो क्लिनिक है: प्लॉट 00-काल्पनिक, डेमो एन्क्लेव, सेक्टर-एक्स, रायपुर, काल्पनिक सिविक पार्क गेट नंबर 9 के सामने। मुफ़्त पार्किंग उपलब्ध है।"),
    ("FAQ-PAYMENT", "Payment", "payment pay upi cash card paisa bhugtan",
     "We accept cash, UPI and debit or credit cards at the reception counter. We never ask for payment, bank details or OTPs over the phone.",
     "हम रिसेप्शन पर नकद, यूपीआई और डेबिट/क्रेडिट कार्ड लेते हैं। हम फ़ोन पर कभी भुगतान, बैंक जानकारी या ओटीपी नहीं माँगते।"),
    ("FAQ-ARRIVAL", "Appointments", "arrive early documents bring kitna pehle aana",
     "Please arrive 15 minutes before a consultation and 20 minutes before an ultrasound, with your booking reference and any previous prescriptions or reports.",
     "कृपया परामर्श से 15 मिनट पहले और अल्ट्रासाउंड से 20 मिनट पहले आएँ, बुकिंग नंबर और पुरानी पर्चियाँ या रिपोर्ट साथ लाएँ।"),
    ("FAQ-CANCEL", "Appointments", "cancel cancellation charge fee radd",
     "You can cancel any time before your appointment at no charge. Just give your booking reference or tell me which appointment.",
     "आप अपॉइंटमेंट से पहले कभी भी बिना शुल्क रद्द कर सकते हैं। बस बुकिंग नंबर बताइए या बताइए कौन-सा अपॉइंटमेंट।"),
    ("FAQ-RESCHEDULE", "Appointments", "reschedule change time badalna date shift",
     "Yes, you can move your appointment to another open slot at no extra cost. You will get a new booking reference.",
     "हाँ, आप अपना अपॉइंटमेंट बिना अतिरिक्त शुल्क के किसी दूसरे खाली स्लॉट में बदल सकते हैं। आपको नया बुकिंग नंबर मिलेगा।"),
    ("FAQ-REPORTS", "Reports", "report result ready collect kab milegi",
     "Ultrasound and ECG reports are given within 30 minutes. Morning blood test reports are ready the same evening after 6:00 PM at reception.",
     "अल्ट्रासाउंड और ईसीजी रिपोर्ट 30 मिनट में मिल जाती है। सुबह के ब्लड टेस्ट की रिपोर्ट उसी शाम 6:00 बजे के बाद रिसेप्शन पर मिलती है।"),
    ("FAQ-HUMAN", "Help", "human staff receptionist person insaan baat",
     "Reception staff are available Monday to Saturday during clinic hours. I can ask them to call you back.",
     "रिसेप्शन स्टाफ सोमवार से शनिवार क्लिनिक समय में उपलब्ध रहता है। मैं उनसे आपको कॉल करने के लिए कह सकती हूँ।"),
    ("FAQ-EMERGENCY", "Safety", "emergency medicine dawa advice symptom ilaaj",
     "I am an administrative assistant and cannot give medical advice. This clinic has no emergency unit. For an emergency call 108 or 112, or go to the nearest hospital.",
     "मैं एक प्रशासनिक सहायक हूँ और चिकित्सा सलाह नहीं दे सकती। इस क्लिनिक में इमरजेंसी यूनिट नहीं है। इमरजेंसी में 108 या 112 पर कॉल करें या नज़दीकी अस्पताल जाएँ।"),
]

PATIENTS = [
    ("PAT-1001", "Rajeshwar Verma", "hi-IN"), ("PAT-1002", "Priyanka Tiwari", "hi-en"),
    ("PAT-1003", "Siddharth Kashyap", "en-IN"), ("PAT-1004", "Kavita Dewangan", "hi-IN"),
    ("PAT-1005", "Amitabh Netam", "hi-en"), ("PAT-1006", "Sunita Patle", "hi-IN"),
    ("PAT-1007", "Vikramaditya Shukla", "en-IN"), ("PAT-1008", "Neha Tamrakar", "hi-en"),
    ("PAT-1009", "Harishankar Yadav", "hi-IN"), ("PAT-1010", "Pooja Banchhor", "hi-en"),
    ("PAT-1011", "Deepak Sonkar", "hi-IN"), ("PAT-1012", "Rituja Agrawal", "en-IN"),
    ("PAT-1013", "Manoj Mandavi", "hi-IN"), ("PAT-1014", "Shweta Koshle", "hi-en"),
    ("PAT-1015", "Gaurav Dadsena", "en-IN"), ("PAT-1016", "Lata Bai Nishad", "hi-IN"),
    ("PAT-1017", "Nikhil Kurre", "hi-en"), ("PAT-1018", "Anjali Markam", "en-IN"),
    ("PAT-1019", "Bhupendra Sinha", "hi-IN"), ("PAT-1020", "Tanuja Thakur", "hi-en"),
]

# (patient, doctor, working-day offset from today, slot index). Offset 0 = today.
FUTURE_BOOKINGS = [
    ("PAT-1001", "DOC-101", 1, 2), ("PAT-1002", "DOC-102", 1, 1), ("PAT-1003", "DOC-101", 1, 20),
    ("PAT-1004", "DOC-103", 1, 0), ("PAT-1005", "DOC-104", 1, 1), ("PAT-1006", "DOC-101", 2, 4),
    ("PAT-1007", "DOC-103", 3, 9), ("PAT-1008", "DOC-102", 2, 3), ("PAT-1009", "DOC-104", 3, 0),
    ("PAT-1010", "DOC-102", 3, 10), ("PAT-1011", "DOC-101", 3, 6), ("PAT-1012", "DOC-104", 4, 2),
]
TODAY_PAST = ["PAT-1013", "PAT-1014", "PAT-1015"]   # COMPLETED earlier today, if the clinic was open
TODAY_FUTURE = ["PAT-1016", "PAT-1017"]             # still to come today, if slots remain
CANCELLED = ("PAT-1019", "DOC-101", 2, 10)


def working_days(start: datetime, count: int) -> list[str]:
    """Today plus the following days, skipping Sundays (clinic closed)."""
    days, d = [], start.date()
    while len(days) < count:
        if d.weekday() != 6:
            days.append(d.isoformat())
        d += timedelta(days=1)
    return days


def seed(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    """Drop everything and load a fresh, internally consistent demo dataset."""
    now = now or now_ist()
    create_schema(conn, drop_existing=True)
    with transaction(conn):
        conn.execute("INSERT INTO clinic VALUES (:id, :name_en, :name_hi, :address_en, :address_hi,"
                     " :landmark, :hours_en, :hours_hi, :payment_modes)", CLINIC)
        for d in DOCTORS:
            conn.execute("INSERT INTO doctors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                         (d["id"], d["name"], d["name_hi"], json.dumps(d["aliases"], ensure_ascii=False),
                          d["specialty"], d["specialty_hi"], d["fee_inr"], d["slot_mins"],
                          json.dumps(d["weekly"]), json.dumps(d["breaks"])))
        for s in SERVICES:
            conn.execute("INSERT INTO services VALUES (:id, :name_en, :name_hi, :fee_inr, :prep_en,"
                         " :prep_hi, :walk_in, :hours_en)", s)
        conn.executemany("INSERT INTO faqs (id, category, keywords, answer_en, answer_hi)"
                         " VALUES (?, ?, ?, ?, ?)", FAQS)
        conn.executemany("INSERT INTO patients VALUES (?, ?, ?, ?)",
                         [(pid, name, lang, f"DEMO-IN-{int(pid[4:]) - 1000:04d}")
                          for pid, name, lang in PATIENTS])

        doctors = {r["id"]: r for r in conn.execute("SELECT * FROM doctors")}
        days = working_days(now, 6)
        today = now.date().isoformat()
        ref_no = 1000
        created = _iso(now - timedelta(days=1))

        def add(patient, doctor_id, day, start, status, reason=None):
            nonlocal ref_no
            ref_no += 1
            conn.execute(
                "INSERT INTO appointments (ref, doctor_id, patient_id, date, start_time, fee_inr,"
                " status, source, cancel_reason, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'SEED', ?, ?, ?)",
                (f"CC-{ref_no}", doctor_id, patient, day, start, doctors[doctor_id]["fee_inr"],
                 status, reason, created, created))

        def pick(doctor_id, day_offset, index, future=True):
            day = days[day_offset] if days[0] == today else days[max(day_offset - 1, 0)]
            slots = schedule_slots(doctors[doctor_id], day)
            if future:
                slots = [s for s in slots if _dt(day, s.start) >= now + BOOKING_LEAD]
            return (day, slots[index % len(slots)].start) if slots else None

        taken: set[tuple[str, str, str]] = set()
        for patient, doctor_id, offset, index in FUTURE_BOOKINGS:
            hit = pick(doctor_id, offset, index)
            if hit and (doctor_id, *hit) not in taken:
                taken.add((doctor_id, *hit))
                add(patient, doctor_id, *hit, "CONFIRMED")

        if days[0] == today:   # the clinic is open today
            past = [s for s in schedule_slots(doctors["DOC-101"], today) if _dt(today, s.start) < now]
            for patient, slot in zip(TODAY_PAST, past[::2]):
                add(patient, "DOC-101", today, slot.start, "COMPLETED")
            upcoming = [s for s in schedule_slots(doctors["DOC-102"], today)
                        if _dt(today, s.start) >= now + BOOKING_LEAD]
            for patient, slot in zip(TODAY_FUTURE, upcoming[1::3]):
                add(patient, "DOC-102", today, slot.start, "CONFIRMED")

        patient, doctor_id, offset, index = CANCELLED
        hit = pick(doctor_id, offset, index)
        if hit and (doctor_id, *hit) not in taken:
            add(patient, doctor_id, *hit, "CANCELLED", "Patient travelling (called to cancel)")

        _seed_calls(conn, now)

    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("doctors", "services", "faqs", "patients", "appointments", "calls", "followups")}
    return counts


def _seed_calls(conn: sqlite3.Connection, now: datetime) -> None:
    """A few earlier calls so the admin call log and follow-up queue aren't empty."""
    calls = [
        ("seed-call-1", "PAT-1004", 180, "hi-IN", "BOOK", "BOOKED", 0,
         "Booked orthopaedic consultation with Dr. Rohan Deshmukh.",
         [("user", "मुझे डॉक्टर देशमुख का अपॉइंटमेंट चाहिए कल सुबह।"),
          ("bot", "डॉक्टर देशमुख कल सुबह 10:00 बजे उपलब्ध हैं, फ़ीस 600 रुपये। क्या मैं बुक कर दूँ?"),
          ("user", "हाँ, कर दीजिए।"),
          ("bot", "आपका अपॉइंटमेंट बुक हो गया है।")]),
        ("seed-call-2", "PAT-1016", 95, "hi-IN", "MEDICAL", "ESCALATED", 1,
         "Caller described chest discomfort and asked about medicine dose; urgent staff callback created.",
         [("user", "सीने में भारीपन है, कौन सी दवा की खुराक बढ़ा दूँ?"),
          ("bot", "मैं दवा की सलाह नहीं दे सकती। इमरजेंसी हो तो तुरंत 108 या 112 पर कॉल करें। मैंने स्टाफ को आपको तुरंत कॉल करने के लिए कहा है।")]),
        ("seed-call-3", "PAT-1009", 60, "hi-en", "HUMAN", "ESCALATED", 1,
         "Caller asked to speak to front desk about a reimbursement receipt.",
         [("user", "Mujhe reception staff se baat karni hai, reimbursement receipt ke baare mein."),
          ("bot", "ठीक है, मैंने reception को आपको call back करने के लिए बोल दिया है।")]),
    ]
    for session, patient, mins_ago, lang, intent, outcome, escalated, summary, turns in calls:
        started = now - timedelta(minutes=mins_ago)
        cur = conn.execute(
            "INSERT INTO calls (session_id, patient_id, started_at, ended_at, language, intent, outcome,"
            " escalated, summary) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session, patient, _iso(started), _iso(started + timedelta(minutes=2)), lang, intent,
             outcome, escalated, summary))
        call_id = cur.lastrowid
        for i, (role, text) in enumerate(turns):
            conn.execute("INSERT INTO turns (call_id, role, text, lang, created_at) VALUES (?, ?, ?, ?, ?)",
                         (call_id, role, text, lang, _iso(started + timedelta(seconds=20 * i))))
        if intent == "MEDICAL":
            conn.execute("INSERT INTO followups (call_id, patient_id, reason, priority, trigger_text,"
                         " created_at) VALUES (?, ?, 'MEDICAL', 'URGENT', ?, ?)",
                         (call_id, patient, turns[0][1], _iso(started + timedelta(seconds=30))))
        elif intent == "HUMAN":
            conn.execute("INSERT INTO followups (call_id, patient_id, reason, priority, status,"
                         " trigger_text, resolution_note, created_at, resolved_at)"
                         " VALUES (?, ?, 'HUMAN_REQUEST', 'NORMAL', 'RESOLVED', ?, ?, ?, ?)",
                         (call_id, patient, turns[0][1], "Called back; receipt ready at counter.",
                          _iso(started + timedelta(seconds=30)), _iso(started + timedelta(minutes=30))))
