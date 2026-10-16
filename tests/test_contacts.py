"""CardDAV transport compatibility and contact field tests."""

import asyncio
from types import SimpleNamespace

import pytest

from urllib3.backend import HttpVersion

from icloud_mcp import contacts


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
    monkeypatch.setattr(contacts, "require_auth", lambda context: ("example@example.com", "test"))
    monkeypatch.setattr(contacts, "_get_carddav_session", lambda *args: (session, "example@example.com"))
    monkeypatch.setattr(contacts, "_discover_principal", lambda *args: "principal")
    monkeypatch.setattr(contacts, "_discover_addressbook_home", lambda *args: "home")
    monkeypatch.setattr(contacts, "_list_addressbooks", lambda *args: [{"url": "book"}])
    monkeypatch.setattr(contacts, "_fetch_all_vcards", lambda *args: [{"url": contact_id, "data": card}])

    listed = asyncio.run(contacts.list_contacts(None))
    fetched = asyncio.run(contacts.get_contact(None, contact_id))
    searched = asyncio.run(contacts.search_contacts(None, "Birthday Example"))

    assert len(listed) == len(searched) == 1
    for contact in (listed[0], fetched, searched[0]):
        assert contact["birthday"] == expected
        assert contact["name"] == "Birthday Example"
