"""Synthetic data generator. EVERYTHING here is fictional and labelled synthetic.

Deterministic (fixed RNG seed) and relative to clock.today(), so the demo always looks "current".
Run: python -m app.seed --reset
"""
from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from app import clock, db
from app.settings import clinic_config

SEED = 20260926


@dataclass
class P:
    full_name: str
    preferred_name: str
    lang: str
    age: int
    recall_type: str
    months_since_last: float
    chas: str = "none"
    gen: str = "none"
    consent: bool = True
    sensitive: bool = False
    sensitive_note: str | None = None
    has_future_appt: bool = False
    followup_due_offset_days: int | None = None   # treatment_followup: due date relative to today


# Fixed demo heroes (ids 1..8) — the video storyline uses these.
HEROES: list[P] = [
    P("Tan Ah Hua (陈亚花)", "陈女士", "zh", 74, "routine_checkup", 24.2, chas="orange", gen="pioneer"),
    P("Mohd Rizal bin Osman", "Encik Rizal", "ms", 52, "scaling_polishing", 13.1, chas="blue"),
    P("Priya d/o Ramasamy", "Priya", "ta", 38, "routine_checkup", 15.0),
    P("Daniel Lee Wei Han", "Daniel", "en", 45, "perio_maintenance", 8.3),
    P("Kelvin Ong Jun Jie", "Kelvin", "en", 33, "routine_checkup", 11.0),
    P("Siti Aminah binte Ali", "Puan Siti", "ms", 67, "denture_review", 20.0, chas="blue", gen="merdeka"),
    P("Lim Guat Eng (林月英)", "林女士", "zh", 81, "treatment_followup", 3.0, chas="green", gen="pioneer",
      followup_due_offset_days=-40),
    P("Murugan s/o Pillai", "Mr Murugan", "ta", 69, "routine_checkup", 30.0, chas="orange", gen="merdeka",
      sensitive=True, sensitive_note="Recently bereaved (spouse). Staff to review every message before it is sent."),
]

CN_SURNAMES = [("Tan", "陈"), ("Lim", "林"), ("Lee", "李"), ("Ng", "黄"), ("Ong", "王"), ("Wong", "黄"), ("Goh", "吴"),
               ("Chua", "蔡"), ("Koh", "许"), ("Teo", "张"), ("Ang", "洪"), ("Yeo", "杨"), ("Tay", "郑"), ("Ho", "何"),
               ("Low", "刘"), ("Toh", "杜"), ("Sim", "沈"), ("Chia", "谢")]
CN_GIVEN_OLD = ["Siew Lan", "Ah Kow", "Kim Hock", "Bee Choo", "Swee Hong", "Chee Keong", "Ah Moi", "Poh Choo",
                "Teck Seng", "Geok Lian", "Hock Seng", "Soo Chin"]
CN_GIVEN_YOUNG = ["Kai Xiang", "Li Ting", "Xin Yi", "Yu Xuan", "Shu Fen", "Wei Ming", "Hui Min", "Zhi Hao",
                  "Jia Hui", "Rui En", "Jun Wei", "Pei Shan"]
CN_ENGLISH = ["Jasmine", "Rachel", "Marcus", "Cheryl", "Bryan", "Vivian", "Nicholas", "Grace", "Adrian", "Michelle"]
MALAY_M = ["Ahmad bin Hassan", "Hafiz bin Abdullah", "Zulkifli bin Yusof", "Iskandar bin Salleh", "Azman bin Kassim",
           "Rahim bin Jaafar", "Faizal bin Hamzah", "Khairul bin Anuar"]
MALAY_F = ["Noraini binte Ahmad", "Fatimah binte Hamid", "Zainab binte Othman", "Aishah binte Karim",
           "Rohani binte Sulaiman", "Salmah binte Ibrahim", "Nurul Huda binte Mohamed", "Halimah binte Yaacob"]
INDIAN_M = ["Arun Kumar s/o Subramaniam", "Rajesh s/o Krishnan", "Suresh s/o Rajan", "Ganesan s/o Muthu",
            "Vijay s/o Ramasamy", "Karthik s/o Selvaraj"]
INDIAN_F = ["Lakshmi d/o Nair", "Kavitha d/o Selvam", "Meena d/o Govindasamy", "Saraswathi d/o Perumal",
            "Deepa d/o Chandran", "Anjali d/o Menon"]
OTHERS = ["Daniel Pereira", "Sarah de Souza", "Michael Rodrigues", "Joanne Oliveiro", "Maria Santos", "Aaron Fernandez"]


def _pick(rng: random.Random, pool: list[str], used: set[str]) -> str:
    choices = [x for x in pool if x not in used] or pool
    x = rng.choice(choices)
    used.add(x)
    return x


def _generate_crowd(rng: random.Random, n: int) -> list[P]:
    people: list[P] = []
    used: set[str] = set()
    communities = ["cn"] * 30 + ["malay"] * 11 + ["indian"] * 10 + ["other"] * 5
    rng.shuffle(communities)
    for i in range(n):
        comm = communities[i % len(communities)]
        age = rng.choice([rng.randint(8, 15), rng.randint(20, 44), rng.randint(45, 64), rng.randint(65, 88),
                          rng.randint(65, 80), rng.randint(25, 60)])
        old = age >= 65
        if comm == "cn":
            sur, sur_cn = rng.choice(CN_SURNAMES)
            if old or rng.random() < 0.25:
                given = _pick(rng, CN_GIVEN_OLD if old else CN_GIVEN_YOUNG, used)
                lang = "zh" if (old and rng.random() < 0.8) or rng.random() < 0.3 else "en"
                full = f"{sur} {given}"
                pref = (f"{sur_cn}{'女士' if rng.random() < 0.5 else '先生'}" if lang == "zh" else given.split()[0])
            else:
                eng = _pick(rng, CN_ENGLISH, used)
                given = _pick(rng, CN_GIVEN_YOUNG, used)
                full, pref, lang = f"{eng} {sur} {given}", eng, "en"
        elif comm == "malay":
            female = rng.random() < 0.5
            full = _pick(rng, MALAY_F if female else MALAY_M, used)
            lang = "ms" if old or rng.random() < 0.45 else "en"
            first = full.split(" bin")[0].split(" binte")[0].split()[0]
            pref = (("Puan " if female else "Encik ") + first) if lang == "ms" else first
        elif comm == "indian":
            female = rng.random() < 0.5
            full = _pick(rng, INDIAN_F if female else INDIAN_M, used)
            lang = "ta" if old or rng.random() < 0.4 else "en"
            pref = full.split(" s/o")[0].split(" d/o")[0].split()[0]
        else:
            full = _pick(rng, OTHERS, used)
            lang, pref = "en", full.split()[0]

        if age <= 15:
            rtype = "child_checkup"
        elif old and rng.random() < 0.2:
            rtype = "denture_review"
        else:
            rtype = rng.choices(["routine_checkup", "scaling_polishing", "perio_maintenance", "treatment_followup"],
                                weights=[58, 22, 10, 10])[0]
        months = rng.choices([rng.uniform(2, 5.5), rng.uniform(6.5, 12), rng.uniform(12, 19), rng.uniform(19, 26),
                              rng.uniform(7, 14)], weights=[22, 25, 23, 10, 20])[0]
        chas = "none"
        gen = "none"
        if old:
            gen = rng.choice(["pioneer", "merdeka", "merdeka", "none"]) if age >= 70 else rng.choice(["merdeka", "none"])
            chas = rng.choice(["blue", "orange", "green", "orange"])
        elif rng.random() < 0.35:
            chas = rng.choice(["blue", "orange", "green"])
        people.append(P(
            full_name=full, preferred_name=pref, lang=lang, age=age, recall_type=rtype, months_since_last=months,
            chas=chas, gen=gen, consent=rng.random() > 0.08,
            has_future_appt=rng.random() < 0.08,
            followup_due_offset_days=rng.randint(-70, 10) if rtype == "treatment_followup" else None,
        ))
    return people


def _add_months(d: date, months: float) -> date:
    return d + timedelta(days=round(months * 30.44))


def build_calendar(rng: random.Random, today: date) -> None:
    cfg = clinic_config()
    cal = cfg["calendar"]
    dentists = [d["id"] for d in cfg["clinic"]["dentists"]]
    rows = []
    for offset in range(1, cal["horizon_days"] + 1):
        d = today + timedelta(days=offset)
        for start, end in cal["sessions"].get(d.weekday(), []):
            t = clock.at(d, start)
            stop = clock.at(d, end)
            while t < stop:
                for did in dentists:
                    status = "blocked" if rng.random() < cal["seed_occupancy"] else "free"
                    rows.append((f"{t.strftime('%Y-%m-%dT%H:%M')}|{did}", did, clock.iso(t), status))
                t += timedelta(minutes=cal["slot_minutes"])
    with db.tx() as c:
        c.executemany("INSERT INTO slots(id,dentist_id,start_ts,status) VALUES(?,?,?,?)", rows)


def seed(n_total: int = 72, reset: bool = True) -> dict[str, int]:
    if reset:
        db.reset_db()
    else:
        db.init_db()
    rng = random.Random(SEED)
    today = clock.today()
    cfg = clinic_config()
    vtypes = cfg["visit_types"]
    dentists = [d["id"] for d in cfg["clinic"]["dentists"]]
    people = HEROES + _generate_crowd(rng, n_total - len(HEROES))
    type_map = cfg.get("seed_type_map") or {}      # e.g. gp.yaml maps routine_checkup → chronic_review

    build_calendar(rng, today)
    free_slots = db.q("SELECT id, dentist_id, start_ts FROM slots WHERE status='free' ORDER BY start_ts")

    with db.tx() as c:
        for i, p in enumerate(people, start=1):
            birth = date(today.year - p.age, rng.randint(1, 12), rng.randint(1, 28))
            phone = f"+655550{i:04d}"
            c.execute(
                "INSERT INTO patients(id,full_name,preferred_name,phone,preferred_language,birth_date,chas_tier,"
                "generation_card,whatsapp_consent,sensitive,sensitive_note,preferred_dentist) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (i, p.full_name, p.preferred_name, phone, p.lang, birth.isoformat(), p.chas, p.gen, int(p.consent),
                 int(p.sensitive), p.sensitive_note, rng.choice(dentists)),
            )
            last = today - timedelta(days=round(p.months_since_last * 30.44))
            rtype = type_map.get(p.recall_type, p.recall_type)
            base_type = type_map.get("routine_checkup", "routine_checkup")
            # a few earlier visits for realism
            for k in range(rng.randint(0, 3), 0, -1):
                c.execute("INSERT INTO visits(patient_id,visit_date,visit_type,dentist_id) VALUES(?,?,?,?)",
                          (i, (last - timedelta(days=180 * k + rng.randint(0, 40))).isoformat(),
                           base_type, rng.choice(dentists)))
            last_type = base_type if vtypes[rtype].get("interval_months") is None else rtype
            c.execute("INSERT INTO visits(patient_id,visit_date,visit_type,dentist_id) VALUES(?,?,?,?)",
                      (i, last.isoformat(), last_type, rng.choice(dentists)))
            if vtypes[rtype].get("interval_months") is None:          # due date set by a clinician's plan
                due, source = today + timedelta(days=p.followup_due_offset_days or -30), "treatment_plan"
            else:
                due, source = _add_months(last, vtypes[rtype]["interval_months"]), "interval"
            c.execute("INSERT INTO recalls(patient_id,visit_type,due_date,source) VALUES(?,?,?,?)",
                      (i, rtype, due.isoformat(), source))
            if p.has_future_appt and free_slots:
                s = free_slots.pop(rng.randrange(len(free_slots)))
                cur = c.execute(
                    "INSERT INTO appointments(patient_id,dentist_id,start_ts,duration_min,visit_type,status,created_by,created_at)"
                    " VALUES(?,?,?,?,?,'booked','seed',?)",
                    (i, s["dentist_id"], s["start_ts"], 30, rtype, clock.iso(clock.now())))
                c.execute("UPDATE slots SET status='booked', appointment_id=? WHERE id=?", (cur.lastrowid, s["id"]))
        c.execute("INSERT OR REPLACE INTO app_settings(key,value) VALUES('seeded_on', ?)", (today.isoformat(),))
    return {"patients": len(people), "slots": len(db.q("SELECT id FROM slots"))}


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate synthetic RecallCare data")
    ap.add_argument("--reset", action="store_true", help="delete and recreate the database")
    ap.add_argument("--n", type=int, default=72)
    args = ap.parse_args()
    out = seed(args.n, reset=args.reset or True)
    print(f"Seeded {out['patients']} synthetic patients and {out['slots']} calendar slots (as of {clock.today()}).")


if __name__ == "__main__":
    main()
