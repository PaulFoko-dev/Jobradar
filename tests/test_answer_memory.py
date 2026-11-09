from datetime import datetime, timedelta, timezone

from jobradar import answer_memory, application_adapters, profile

NOW = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)


def test_answers_expire_by_category() -> None:
    availability = answer_memory.stamp("availability", {"answer": "Janvier 2027"}, NOW)
    licence = answer_memory.stamp("driving_licence", {"answer": "Oui"}, NOW)
    assert not answer_memory.is_expired(availability, NOW + timedelta(days=29))
    assert answer_memory.is_expired(availability, NOW + timedelta(days=31))
    assert licence["expires_at"] is None and not answer_memory.is_expired(licence, NOW + timedelta(days=3650))


def test_a_changed_answer_creates_a_version_and_unchanged_ones_keep_their_dates() -> None:
    first = answer_memory.merge({}, {"desired_salary": {"question": "Salaire", "answer": "1 200 €"}}, NOW)
    later = NOW + timedelta(days=100)
    same = answer_memory.merge(first, {"desired_salary": {"question": "Salaire", "answer": "1 200 €"}}, later)
    assert same["desired_salary"]["validated_at"] == first["desired_salary"]["validated_at"]
    assert answer_memory.is_expired(same["desired_salary"], later)  # resté expiré : pas de revalidation muette
    changed = answer_memory.merge(same, {"desired_salary": {"question": "Salaire", "answer": "1 400 €"}}, later)
    assert changed["desired_salary"]["answer"] == "1 400 €"
    assert changed["desired_salary"]["history"][0] == {"answer": "1 200 €", "validated_at": "2026-11-09T12:00:00Z",
                                                       "status": "remplacé"}
    assert not answer_memory.is_expired(changed["desired_salary"], later)


def test_expired_answers_are_not_used_by_the_robot(monkeypatch) -> None:
    old = {"question": "Date de disponibilité", "answer": "Septembre 2026", "validated_at": "2026-01-01T00:00:00Z",
           "expires_at": "2026-01-31T00:00:00Z"}
    candidate = {"standard_answers": {"availability": old}}
    assert application_adapters.standard_answer_for({"label": "Start date", "input_kind": "text"}, candidate) is None


def test_form_answers_are_filed_under_their_concept_and_can_be_confirmed(data_dir) -> None:
    profile.remember_form_answers([{"label": "When can you start?", "value": "Janvier 2027"}])
    saved = profile.load_profile()["standard_answers"]
    assert saved["availability"]["answer"] == "Janvier 2027" and saved["availability"]["expires_at"]
    confirmed = profile.confirm_answer("availability")["standard_answers"]["availability"]
    assert confirmed["validated_at"] >= saved["availability"]["validated_at"]
    rows = {row["key"]: row for row in answer_memory.view(profile.load_profile()["standard_answers"])}
    assert rows["availability"]["state"] == "active"
