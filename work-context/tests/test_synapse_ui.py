"""Unit coverage for the Synapse UI glue — nav routing, proxy route table,
and the pages server's stable field contracts. DB-backed report functions are
integration-tested via the running app; here we cover the pure logic."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "derive"))

import synapse_nav
import synapse_pages
import synapse_server


def test_active_from_path():
    assert synapse_nav.active_from_path("/people") == "people"
    assert synapse_nav.active_from_path("/") == "home"
    assert synapse_nav.active_from_path("/v5") == "home"
    assert synapse_nav.active_from_path("/pr-friction") == "pr"
    assert synapse_nav.active_from_path("/topics") == "topics"
    assert synapse_nav.active_from_path("/sprint-planner-v2.html") == "sprint"
    assert synapse_nav.active_from_path("/nope") == ""


def test_build_nav_groups_and_active():
    nav = synapse_nav.build_nav("people")
    for group in ("Overview", "Planning", "Delivery", "Insight"):
        assert group in nav
    assert 'href="/people" class="active"' in nav
    # every registered page key appears as a link
    for _, links in synapse_nav.GROUPS:
        for href, _key, _label in links:
            assert f'href="{href}"' in nav


def test_proxy_route_partitions_are_disjoint():
    # A dashboard route must never also be a pages prefix (would be ambiguous).
    for r in synapse_server.DASH_ROUTES:
        assert not r.startswith(synapse_server.PAGES_PREFIXES)
    assert "/people" in synapse_server.PAGES_PREFIXES
    assert "/" in synapse_server.DASH_ROUTES


def test_pages_registered_for_every_nav_link():
    # Every pages-server nav link resolves to a registered page or an API prefix.
    served = set(synapse_pages.PAGES)
    for href in ("/people", "/pr-friction", "/releases", "/docs",
                 "/meetings", "/ask", "/topics", "/velocity", "/services", "/timeline"):
        assert href in served


def test_people_bucket_contract():
    assert synapse_pages._FIELDS  # non-empty
    assert "prsOpened" in synapse_pages._FIELDS
    # every bucket target is a declared field
    for target in synapse_pages._BUCKET.values():
        assert target in synapse_pages._FIELDS
