from __future__ import annotations

import gzip
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from norway_company_agent.nav_jobs import NavJobIndex, jobs_for_profile  # noqa: E402
from norway_company_agent.site_intel import (  # noqa: E402
    is_ats_ad, is_career_url, is_post_url, job_postings_from_jsonld, parse_sitemap,
)


def test_post_urls_need_a_section_and_a_slug():
    assert is_post_url("https://firma.no/nyheter/ny-avdeling-i-sandnes/")
    assert is_post_url("https://firma.no/no/artikler/ville-vekster")
    assert not is_post_url("https://firma.no/nyheter/")
    assert not is_post_url("https://firma.no/nyheter/page/2")
    assert not is_post_url("https://firma.no/produkter/lampe")


def test_career_urls_ignore_products_that_mention_jobs():
    assert is_career_url("https://malvet.no/jobb-hos-oss/")
    assert is_career_url("https://firma.no/om-oss/ledige-stillinger")
    assert is_career_url("https://firma.no/side?id=4", "Karriere")
    assert not is_career_url("https://tronsmo.no/produkt/arbetet-pa-jobbet/")
    assert not is_career_url("https://tronsmo.no/produkt/bullshit-jobs-rise-of-pointless-jobs-and/")


def test_recruiting_system_ad_links():
    assert is_ats_ad("https://arbeidsplassen.nav.no/stillinger/stilling/ff651cd0-a816-4ce3-b1d2-3c4d5e6f7a8b")
    assert is_ats_ad("https://www.finn.no/job/fulltime/ad.html?finnkode=312345678")
    assert is_ats_ad("https://firma.teamtailor.com/jobs/123456-elektriker")
    assert not is_ats_ad("https://www.finn.no/job/browse.html")


def test_parse_sitemap_index_and_gzip():
    index = b"<sitemapindex><sitemap><loc>https://a.no/post-sitemap.xml</loc></sitemap></sitemapindex>"
    assert parse_sitemap(index) == ([], ["https://a.no/post-sitemap.xml"])
    urls = b"<urlset><url><loc>https://a.no/nyheter/x-y-z</loc><lastmod>2026-09-01</lastmod></url></urlset>"
    assert parse_sitemap(gzip.compress(urls))[0] == [{"url": "https://a.no/nyheter/x-y-z", "lastmod": "2026-09-01"}]


def test_jobposting_jsonld():
    data = {"json-ld": [{"@type": "JobPosting", "title": "Lærling elektro", "datePosted": "2026-09-20",
                         "hiringOrganization": {"name": "Vinsnesbakk Elektro AS"},
                         "jobLocation": {"address": {"addressLocality": "Sandnes"}}}]}
    jobs = job_postings_from_jsonld(data, "https://v.no/karriere")
    assert jobs[0]["title"] == "Lærling elektro" and jobs[0]["work_locations"] == [{"addressLocality": "Sandnes"}]


def test_site_jobs_merge_without_double_counting_nav():
    profile = {"organisation_number": "912345678", "name": "VINSNESBAKK ELEKTRO AS", "evidence": {}}
    site_jobs = [{"title": "Elektriker", "url": "https://v.teamtailor.com/jobs/1-elektriker", "listed_on": "https://v.no/karriere"},
                 {"title": "NAV ad", "url": "https://arbeidsplassen.nav.no/stillinger/stilling/00000000-0000-0000-0000-000000000001"}]
    record, _ = jobs_for_profile(profile, NavJobIndex({}, None), lambda uuid: {}, today=date(2026, 10, 10),
                                 site_jobs=site_jobs, site_observations=[{"item_url": site_jobs[0]["url"]}])
    assert record["status"] == "available" and record["value"]["active_job_count"] == 1
    assert record["value"]["company_site_postings"][0]["url"].endswith("1-elektriker")


def test_activity_only_from_the_companys_own_site():
    from norway_company_agent.site_intel import own_site_reason
    def value(domain, title="", score=0.95):
        return {"registered_domain": domain, "title": title, "identity_assessment": {"score": score}}
    assert own_site_reason({"name": "CARGONET AS"}, value("cargonet.no"))
    assert own_site_reason({"name": "BLOMEN HAGE OG LANDSKAP AS"}, value("blomenhage.no"))
    assert own_site_reason({"name": "TREDJE BOLIGSEKTOR AS"}, value("hellvikhage.no", "Shared Living - Tredje Boligsektor AS"))
    assert own_site_reason({"name": "MIDTUNKRAFT AS"}, value("clemenskraft.no", "Home | Clemens Kraft", 0.97)) is None
    assert own_site_reason({"name": "SAMEIET WALDEMAR THRANESGATE 40"}, value("ecitnorian.com", "Ecit Norian")) is None


def test_parse_feed_reads_rss_and_atom_and_skips_undated():
    from norway_company_agent.site_intel import parse_feed

    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Nyheter</title>
    <item><title><![CDATA[Ny avdeling i Sandnes]]></title><link>https://example.no/nyheter/ny-avdeling</link>
    <pubDate>Tue, 06 Oct 2026 08:00:00 +0200</pubDate></item>
    <item><title>Uten dato</title><link>https://example.no/nyheter/x</link></item></channel></rss>"""
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Vi vant kontrakt</title>
    <link rel="alternate" href="https://example.no/a"/><published>2026-09-01T10:00:00Z</published></entry></feed>"""
    assert parse_feed(rss) == [{"title": "Ny avdeling i Sandnes", "url": "https://example.no/nyheter/ny-avdeling",
                                "published": "2026-10-06T08:00:00+02:00"}]
    assert parse_feed(atom)[0]["url"] == "https://example.no/a"
    assert parse_feed(b"<html><body>not a feed</body></html>") == []


def test_post_filters_drop_attachments_archives_and_other_locales():
    from norway_company_agent.site_intel import locale_prefix, post_allowed

    assert locale_prefix("https://www.kompan.com/nb/no") == "/nb/no" and locale_prefix("https://firma.no/") == ""
    assert not post_allowed("https://www.kompan.com/cs/cz/about/news/x", "Novy katalog", "/nb/no")
    assert post_allowed("https://www.kompan.com/nb/no/om-oss/nyheter/x", "Ny katalog", "/nb/no")
    assert not post_allowed("https://sprakreisebyraet.no/slide/blog/blog-temp-image3/", "blog-temp-image3", "")
    assert not post_allowed("https://firma.no/nyheter/administrasjon", "Administrasjon Arkiver - Firma AS", "")
    assert post_allowed("https://firma.no/nyheter/nytt-lokale", "Nytt lokale", "")
    assert not post_allowed("https://firma.no/hello-world/", "Hello world!", "")
