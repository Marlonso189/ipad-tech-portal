# SPDX-License-Identifier: AGPL-3.0-or-later
"""Jamf URL normalization. No network."""

import pytest

from app.jamf_url import JamfUrlError, normalize_jamf_url


def test_https_url_drops_path_query_and_default_port():
    url, host = normalize_jamf_url("https://Jamf.Example.ORG:443/jamf/ui/?x=1#frag", allow_http=False)
    assert url == "https://jamf.example.org"
    assert host == "jamf.example.org"


def test_non_default_port_is_kept_and_trailing_slash_is_removed():
    url, host = normalize_jamf_url("  https://jamf.example.org:8443/  ", allow_http=False)
    assert url == "https://jamf.example.org:8443"
    assert host == "jamf.example.org:8443"


def test_http_is_rejected_unless_test_mode_allows_it():
    with pytest.raises(JamfUrlError, match="https://"):
        normalize_jamf_url("http://jamf.example.org/jamf", allow_http=False)
    url, host = normalize_jamf_url("http://127.0.0.1:8742/jamf/", allow_http=True)
    assert url == "http://127.0.0.1:8742"
    assert host == "127.0.0.1:8742"


def test_missing_scheme_userinfo_and_blank_are_rejected():
    with pytest.raises(JamfUrlError, match="https://"):
        normalize_jamf_url("jamf.example.org", allow_http=False)
    with pytest.raises(JamfUrlError, match="username or password"):
        normalize_jamf_url("https://tech:s3cret@jamf.example.org", allow_http=False)
    with pytest.raises(JamfUrlError, match="Enter the Jamf Pro server URL"):
        normalize_jamf_url("   ", allow_http=False)


def test_error_does_not_echo_the_typed_value():
    raw = "https://tech:s3cret-not-stored@jamf.example.org/secret"
    with pytest.raises(JamfUrlError) as caught:
        normalize_jamf_url(raw, allow_http=False)
    assert "s3cret-not-stored" not in str(caught.value)
    assert raw not in str(caught.value)
