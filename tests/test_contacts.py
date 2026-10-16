"""CardDAV transport compatibility and contact field tests."""

import asyncio
from types import SimpleNamespace

import pytest

from urllib3.backend import HttpVersion

from icloud_mcp import auth, contacts


def test_carddav_session_disables_http3(monkeypatch):
    monkeypatch.setattr(contacts.config, "DISABLE_HTTP3", True)
    session, email = contacts._get_carddav_session("person@example.com", "secret")

    assert email == "person@example.com"
    assert session.get_adapter("https://").poolmanager.connection_pool_kw["disabled_svn"] == {
        HttpVersion.h3
    }


@pytest.mark.parametrize("bday, expected", [
    ("BDAY:1985-04-12\n", "1985-04-12"),
    ("BDAY:19850412\n", "19850412"),
    ("BDAY:--0412\n", "--0412"),
    ("BDAY:--04-12\n", "--04-12"),
    ("BDAY;VALUE=text:circa 1800\n", "circa 1800"),
    ("BDAY:\n", None),
    ("", None),
])
def test_birthday_in_contact_read_tools(monkeypatch, bday, expected):
    """Exercise real vCard parsing through list, get and search without Apple."""
    card = "BEGIN:VCARD\nVERSION:3.0\nFN:Birthday Example\nN:Example;Birthday;;;\n" + bday + "END:VCARD\n"
    contact_id = "https://contacts.icloud.com/book/example.vcf"
    response = SimpleNamespace(text=card, raise_for_status=lambda: None)
    session = SimpleNamespace(get=lambda *args, **kwargs: response)
    monkeypatch.setattr(auth, "get_http_headers", dict)
    monkeypatch.setattr(contacts.config, "CONTACTS_EMAIL", "contacts@example.com")
    monkeypatch.setattr(contacts.config, "CONTACTS_PASSWORD", "contacts-secret")
    monkeypatch.setattr(contacts.config, "FALLBACK_EMAIL", "calendar@example.com")
    monkeypatch.setattr(contacts.config, "FALLBACK_PASSWORD", "calendar-secret")

    def carddav_session(email, password):
        assert (email, password) == ("contacts@example.com", "contacts-secret")
        return session, email

    monkeypatch.setattr(contacts, "_get_carddav_session", carddav_session)
    monkeypatch.setattr(contacts, "_discover_principal", lambda *args: "principal")
    monkeypatch.setattr(contacts, "_discover_addressbook_home", lambda *args: "home")
    monkeypatch.setattr(contacts, "_list_addressbooks", lambda *args: [{"url": "book"}])
    monkeypatch.setattr(contacts, "_fetch_all_vcards", lambda *args: [{"url": contact_id, "data": card}])

    listed = asyncio.run(contacts.list_contacts(None))
    fetched = asyncio.run(contacts.get_contact(None, contact_id))
    searched = asyncio.run(contacts.search_contacts(None, "Birthday Example"))
    birthdays = asyncio.run(contacts.list_birthdays(None, month=4, day=12))

    assert len(listed) == len(searched) == 1
    for contact in (listed[0], fetched, searched[0]):
        assert contact["birthday"] == expected
        assert contact["name"] == "Birthday Example"
    if expected and expected != "circa 1800":
        assert birthdays == [{"id": contact_id, "name": "Birthday Example", "birthday": expected}]
    else:
        assert birthdays == []


@pytest.mark.parametrize("birthday,query,match", [
    ("1985-10-20", (10, 20), True),
    ("20001020", (10, 20), True),
    ("--1020", (10, 20), True),
    ("--10-20", (10, 20), True),
    ("19851020T231000Z", (10, 20), True),
    ("1985-10-20T23:10:00-05:00", (10, 20), True),
    ("2000-02-29", (2, 29), True),
    ("--0229", (2, 29), True),
    ("--02-29", (2, 29), True),
    ("2001-02-29", (2, 29), False),
    ("1985-10-21", (10, 20), False),
    ("--1032", (10, 20), False),
    ("--0431", (4, 30), False),
    ("--10", (10, 20), False),
    ("circa 1800", (10, 20), False),
    (None, (10, 20), False),
    ("", (10, 20), False),
])
def test_birthday_filter_formats_and_leap_days(monkeypatch, birthday, query, match):
    async def list_contacts(context):
        return [{"id": "example", "name": "Example", "birthday": birthday, "emails": ["private@example.com"]}]

    monkeypatch.setattr(contacts, "list_contacts", list_contacts)
    result = asyncio.run(contacts.list_birthdays(None, *query))
    assert result == ([{"id": "example", "name": "Example", "birthday": birthday}] if match else [])


def test_birthday_filter_returns_all_matches_sorted_by_name(monkeypatch):
    calls = []

    async def list_contacts(context):
        calls.append(context)
        return [
            {"id": "z", "name": "Zoe", "birthday": "1990-10-20"},
            {"id": "other", "name": "Other", "birthday": "1990-10-21"},
            {"id": "a", "name": "alice", "birthday": "--1020"},
        ]

    monkeypatch.setattr(contacts, "list_contacts", list_contacts)
    for _ in range(2):
        result = asyncio.run(contacts.list_birthdays(None, 10, 20))
        assert [contact["id"] for contact in result] == ["a", "z"]
    assert len(calls) == 2  # Every call fetches fresh contacts.


@pytest.mark.parametrize("month,day", [(0, 1), (13, 1), (1, 0), (1, 32), (4, 31), (2, 30), (True, 1), (1, 1.5)])
def test_invalid_birthday_query_does_not_fetch_contacts(monkeypatch, month, day):
    async def unexpected_fetch(context):
        pytest.fail("Invalid dates must be rejected before contacting iCloud")

    monkeypatch.setattr(contacts, "list_contacts", unexpected_fetch)
    with pytest.raises(contacts.InvalidBirthdayDate):
        asyncio.run(contacts.list_birthdays(None, month, day))
