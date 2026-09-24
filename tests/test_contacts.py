"""CardDAV transport compatibility tests."""

from urllib3.backend import HttpVersion

from icloud_mcp import contacts


def test_carddav_session_disables_http3(monkeypatch):
    monkeypatch.setattr(contacts.config, "DISABLE_HTTP3", True)
    session, email = contacts._get_carddav_session("person@example.com", "secret")

    assert email == "person@example.com"
    assert session.get_adapter("https://").poolmanager.connection_pool_kw["disabled_svn"] == {
        HttpVersion.h3
    }
