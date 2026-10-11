from norway_company_agent.website import http_fallback_url


def test_bare_https_homepage_falls_back_to_http():
    assert http_fallback_url("https://verbena.no/", True) == "http://verbena.no/"


def test_homepage_without_scheme_falls_back_to_http():
    assert http_fallback_url("https://kcs.no", False) == "http://kcs.no"


def test_deep_https_link_and_plain_http_do_not_fall_back():
    assert http_fallback_url("https://example.no/om-oss", True) is None
    assert http_fallback_url("http://example.no/", True) is None
