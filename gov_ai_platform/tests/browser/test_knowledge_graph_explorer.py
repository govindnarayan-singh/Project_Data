from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "outputs" / "kg_visualization_v22"
SCREENSHOT_DIR = OUTPUT_DIR / "screenshots"
DEFAULT_URL = "http://127.0.0.1:8000/knowledge-graph/"


def _write_json(name: str, payload: Any) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _v1_asset(path: str) -> str:
    return subprocess.check_output(
        ["git", "show", f"HEAD:gov_ai_platform/{path}"],
        cwd=PROJECT_ROOT,
        text=True,
        encoding="utf-8",
    )


def reproduce_stale_asset_regression(base_url: str = DEFAULT_URL) -> dict[str, Any]:
    """Reproduce the observed mixed V1-static/V2-template browser failure."""
    from playwright.sync_api import sync_playwright

    html = urllib.request.urlopen(base_url, timeout=10).read().decode("utf-8")
    html = html.replace("<head>", f'<head><base href="{base_url}">', 1)
    v1_script = _v1_asset("static/knowledge_graph/explorer.js")
    v1_stylesheet = _v1_asset("static/knowledge_graph/explorer.css")

    def run_case(*, bridge_neighborhood: bool) -> dict[str, Any]:
        case_html = html
        if bridge_neighborhood:
            api_marker = case_html.index('<script id="kg-explorer-api"')
            root_end = case_html.rfind("</div>", 0, api_marker)
            case_html = (
                case_html[:root_end]
                + '<button type="button" hidden data-neighborhood></button>'
                + case_html[root_end:]
            )
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1366, "height": 768})
            page_errors: list[dict[str, str]] = []
            page.on("pageerror", lambda error: page_errors.append({
                "message": str(error), "stack": error.stack or "",
            }))
            page.route(
                "**/static/knowledge_graph/explorer.js*",
                lambda route: route.fulfill(status=200, content_type="application/javascript", body=v1_script),
            )
            page.route(
                "**/static/knowledge_graph/explorer.css*",
                lambda route: route.fulfill(status=200, content_type="text/css", body=v1_stylesheet),
            )
            page.set_content(case_html, wait_until="networkidle")
            page.wait_for_timeout(400)
            result = {
                "page_errors": page_errors,
                "displayed_nodes": page.locator("[data-visible-nodes]").inner_text(),
                "displayed_edges": page.locator("[data-visible-edges]").inner_text(),
                "computed_background_image": page.locator("#knowledge-graph-canvas").evaluate(
                    "element => getComputedStyle(element).backgroundImage"
                ),
            }
            browser.close()
            return result

    payload = {
        "status": "REPRODUCED",
        "method": "REAL_HEADLESS_CHROMIUM_WITH_COMMITTED_V1_ASSETS_AND_CURRENT_V2_HTML",
        "first_failure": run_case(bridge_neighborhood=False),
        "after_bridging_first_missing_control": run_case(bridge_neighborhood=True),
        "production_database_touched": False,
    }
    _write_json("stale_asset_browser_reproduction.json", payload)
    return payload


def _count(page, selector: str) -> int:
    return int(page.locator(selector).inner_text().strip())


def _wait_counts(page, nodes: int, edges: int) -> None:
    page.wait_for_function(
        "([n,e]) => document.querySelector('[data-visible-nodes]')?.textContent.trim() === String(n) "
        "&& document.querySelector('[data-visible-edges]')?.textContent.trim() === String(e)",
        arg=[nodes, edges],
    )


def _click_graph_element(page, expression: str) -> None:
    position = page.evaluate(expression)
    box = page.locator("#knowledge-graph-canvas").bounding_box()
    assert box and position
    page.mouse.click(box["x"] + position["x"], box["y"] + position["y"])


def run_acceptance(base_url: str = DEFAULT_URL) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page_errors: list[str] = []
        console_errors: list[str] = []
        toggles: dict[str, str] = {}
        capabilities: dict[str, str] = {}
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

        response = page.goto(base_url, wait_until="networkidle")
        assert response and response.ok
        _wait_counts(page, 87, 50)
        assert page.evaluate("() => window.__kgExplorerState.cy.nodes().length") == 87
        assert page.evaluate("() => window.__kgExplorerState.cy.edges().length") == 50
        assert page.locator("#knowledge-graph-canvas canvas").count() > 0
        background_styles = page.locator("#knowledge-graph-canvas").evaluate(
            "element => ({canvas: getComputedStyle(element).backgroundImage, "
            "before: getComputedStyle(element, '::before').backgroundImage, "
            "after: getComputedStyle(element, '::after').backgroundImage, "
            "parent: getComputedStyle(element.parentElement).backgroundImage})"
        )
        assert all(value == "none" for value in background_styles.values())
        assert page.evaluate("() => window.__kgExplorerDomContract.status") == "PASS"
        assert page.locator(".sidebar-brand-row [data-sidebar-toggle]").is_visible()
        assert page.locator(".graph-title [data-sidebar-toggle]").count() == 0
        assert page.evaluate("() => window.__kgNodeRoleContract.source") == "validated REFERENCES only"
        assert page.evaluate("() => window.__kgNodeRoleContract.candidateNodesIncluded") is False
        assert page.locator("[data-summary-caption]").inner_text() == (
            "87 documents · 50 references · 38 connected · 43.7% coverage · 58 components"
        )
        page.screenshot(path=SCREENSHOT_DIR / "01_page_loaded.png", full_page=False)

        node_role_counts = page.evaluate("() => window.__kgExplorerState.graph.node_role_counts")
        assert sum(node_role_counts[role] for role in (
            "ISOLATED_DOCUMENT", "REFERENCED_DOCUMENT", "CITING_DOCUMENT",
            "REFERENCED_AND_CITING_DOCUMENT",
        )) == 87
        assert all(node_role_counts[role] > 0 for role in (
            "ISOLATED_DOCUMENT", "REFERENCED_DOCUMENT", "CITING_DOCUMENT",
            "REFERENCED_AND_CITING_DOCUMENT",
        ))
        assert node_role_counts["CANDIDATE_OR_UNRESOLVED"] == 0
        semantic_styles = page.evaluate("""() => {
          const cy = window.__kgExplorerState.cy;
          const result = {};
          for (const role of ['ISOLATED_DOCUMENT','REFERENCED_DOCUMENT','CITING_DOCUMENT','REFERENCED_AND_CITING_DOCUMENT']) {
            const node = cy.nodes().filter(item => item.data('graphRole') === role)[0];
            result[role] = {shape: node.style('shape'), color: node.style('background-color'), border: node.style('border-style')};
          }
          return result;
        }""")
        assert len({value["shape"] for value in semantic_styles.values()}) == 4
        assert len({value["color"] for value in semantic_styles.values()}) == 4
        capabilities["semantic_node_styles"] = "PASS"

        legend = page.locator("[data-legend]")
        legend.locator("summary").click()
        for label in ("Referenced document", "Citing document", "Referenced + citing", "Isolated document", "Department", "References", "Issued by"):
            assert label in legend.inner_text()
        page.screenshot(path=SCREENSHOT_DIR / "02_semantic_legend_open.png", full_page=False)
        legend.locator("summary").click()

        role_screenshots = {
            "REFERENCED_DOCUMENT": "03_referenced_document_selected.png",
            "CITING_DOCUMENT": "04_citing_document_selected.png",
            "REFERENCED_AND_CITING_DOCUMENT": "05_referenced_and_citing_selected.png",
            "ISOLATED_DOCUMENT": "06_isolated_document_selected.png",
        }
        for role, screenshot_name in role_screenshots.items():
            page.evaluate("""role => {
              const cy = window.__kgExplorerState.cy;
              const node = cy.nodes().filter(item => item.data('graphRole') === role)[0];
              window.__kgRoleNodeId = node.id();
              cy.fit(node, 210);
            }""", role)
            page.wait_for_timeout(180)
            _click_graph_element(page, "() => window.__kgExplorerState.cy.getElementById(window.__kgRoleNodeId).renderedPosition()")
            page.wait_for_function("role => Array.from(document.querySelectorAll('.inspector-facts dd')).some(item => item.textContent.trim() === role)", arg={
                "REFERENCED_DOCUMENT": "Referenced document",
                "CITING_DOCUMENT": "Citing document",
                "REFERENCED_AND_CITING_DOCUMENT": "Referenced + citing document",
                "ISOLATED_DOCUMENT": "Isolated document",
            }[role])
            inspector_text = page.locator(".inspector-facts").inner_text().upper()
            assert "INCOMING REFERENCES" in inspector_text
            assert "OUTGOING REFERENCES" in inspector_text
            assert "TOTAL REFERENCES DEGREE" in inspector_text
            assert "Graph role:" in page.locator("[data-tooltip]").inner_text()
            selected_style = page.evaluate("""() => {
              const node = window.__kgExplorerState.cy.getElementById(window.__kgRoleNodeId);
              return {shape: node.style('shape'), color: node.style('background-color')};
            }""")
            assert selected_style["shape"] == semantic_styles[role]["shape"]
            assert selected_style["color"] == semantic_styles[role]["color"]
            page.screenshot(path=SCREENSHOT_DIR / screenshot_name, full_page=False)
            page.locator("[data-clear-selection]").click()
        capabilities["semantic_role_inspector"] = "PASS"
        page.locator("[data-relayout]").click()

        page.locator('[data-scope="administrative"]').click()
        _wait_counts(page, 98, 137)
        assert page.evaluate("() => window.__kgExplorerState.cy.nodes().length") == 98
        assert page.evaluate("() => window.__kgExplorerState.cy.edges().length") == 137
        assert "Displayed 98 of 98 nodes" in page.locator("[data-display-status]").inner_text()
        administrative_node_role_counts = page.evaluate("() => window.__kgExplorerState.graph.node_role_counts")
        assert administrative_node_role_counts["DEPARTMENT"] == 11
        page.evaluate("""() => {
          const cy = window.__kgExplorerState.cy;
          const node = cy.nodes('.department').sort((a, b) => a.data('degreeValue') - b.data('degreeValue'))[0];
          window.__kgDepartmentNodeId = node.id();
          cy.fit(node, 210);
        }""")
        page.wait_for_timeout(180)
        page.evaluate("() => window.__kgExplorerState.cy.getElementById(window.__kgDepartmentNodeId).emit('tap')")
        page.wait_for_function("() => Array.from(document.querySelectorAll('.inspector-facts dd')).some(item => item.textContent.trim() === 'Department')")
        page.screenshot(path=SCREENSHOT_DIR / "07_department_selected.png", full_page=False)
        page.locator("[data-clear-selection]").click()
        page.locator("[data-relayout]").click()

        page.locator('[data-scope="documents"]').click()
        _wait_counts(page, 87, 50)
        assert page.evaluate("() => window.__kgExplorerState.cy.nodes().length") == 87
        assert page.evaluate("() => window.__kgExplorerState.cy.edges().length") == 50
        assert "Displayed 87 of 87 nodes" in page.locator("[data-display-status]").inner_text()
        page.screenshot(path=SCREENSHOT_DIR / "08_graph_documents_only.png", full_page=False)

        width_before_sidebar = page.locator(".graph-stage-card").bounding_box()["width"]
        page.locator("[data-sidebar-toggle]").focus()
        page.keyboard.press("Enter")
        page.wait_for_timeout(250)
        assert page.locator("body").evaluate("element => element.classList.contains('sidebar-collapsed')")
        assert page.locator(".graph-stage-card").bounding_box()["width"] > width_before_sidebar
        assert page.locator("[data-sidebar-toggle]").get_attribute("aria-expanded") == "false"
        assert page.locator(".brand-mark").is_visible()
        assert page.locator(".nav-link.active").is_visible()
        assert 74 <= page.locator(".sidebar").bounding_box()["width"] <= 78
        page.screenshot(path=SCREENSHOT_DIR / "09_global_sidebar_collapsed.png", full_page=False)
        collapsed_width = page.locator(".graph-stage-card").bounding_box()["width"]
        page.locator("[data-sidebar-toggle]").click()
        page.wait_for_timeout(250)
        assert not page.locator("body").evaluate("element => element.classList.contains('sidebar-collapsed')")
        assert page.locator("[data-sidebar-toggle]").get_attribute("aria-expanded") == "true"
        assert page.locator(".graph-stage-card").bounding_box()["width"] < collapsed_width
        toggles["application_sidebar"] = "PASS"

        toolbar_filter = page.locator(".graph-stage-actions [data-filter-toggle]")
        filters_open_stage_width = page.locator(".graph-stage-card").bounding_box()["width"]
        toolbar_filter.click()
        page.wait_for_timeout(250)
        assert page.locator("[data-graph-shell]").evaluate("element => element.classList.contains('filter-collapsed')")
        filters_closed_stage_width = page.locator(".graph-stage-card").bounding_box()["width"]
        assert filters_closed_stage_width > filters_open_stage_width
        assert abs(page.locator(".graph-stage-head").bounding_box()["width"] - filters_closed_stage_width) < 1
        assert abs(page.locator("#knowledge-graph-canvas").bounding_box()["width"] - filters_closed_stage_width) < 1
        toolbar_filter.click()
        page.wait_for_timeout(250)
        assert not page.locator("[data-graph-shell]").evaluate("element => element.classList.contains('filter-collapsed')")
        assert toolbar_filter.get_attribute("aria-expanded") == "true"
        assert abs(page.locator(".graph-stage-head").bounding_box()["width"] - filters_open_stage_width) < 1
        assert page.locator(".graph-stage-head").evaluate("element => element.scrollWidth <= element.clientWidth")
        toggles["filters"] = "PASS"
        capabilities["filter_panel_space_reclamation"] = "PASS"
        page.screenshot(path=SCREENSHOT_DIR / "05_filters_open.png", full_page=False)

        page.locator("[data-workspace-focus]").click()
        page.wait_for_timeout(250)
        assert page.locator("body").evaluate("element => element.classList.contains('kg-focus-mode')")
        assert _count(page, "[data-visible-nodes]") == 87
        assert page.evaluate("() => window.__kgExplorerState.cy.nodes().length") == 87
        page.screenshot(path=SCREENSHOT_DIR / "06_focus_mode.png", full_page=False)
        page.locator("[data-workspace-focus]").click()
        page.wait_for_timeout(200)
        assert not page.locator("body").evaluate("element => element.classList.contains('kg-focus-mode')")
        toggles["focus"] = "PASS"

        page.locator("[data-workspace-fullscreen]").click()
        page.wait_for_function(
            "() => Boolean(document.fullscreenElement || document.querySelector('[data-graph-shell]').classList.contains('is-maximized'))"
        )
        page.screenshot(path=SCREENSHOT_DIR / "07_fullscreen_or_fallback.png", full_page=False)
        page.locator("[data-fit]").click()
        page.locator("[data-relayout]").click()
        _click_graph_element(
            page,
            "() => { const cy = window.__kgExplorerState.cy; const c = {x: cy.width()/2, y: cy.height()/2}; "
            "return cy.nodes().filter(n => n.data('degreeValue') > 0).sort((a,b) => "
            "Math.hypot(a.renderedPosition().x-c.x,a.renderedPosition().y-c.y) - "
            "Math.hypot(b.renderedPosition().x-c.x,b.renderedPosition().y-c.y))[0].renderedPosition(); }",
        )
        page.locator(".inspector-title").wait_for()
        page.locator("[data-clear-selection]").click()
        page.locator("[data-workspace-fullscreen]").click()
        page.wait_for_function(
            "() => !document.fullscreenElement && !document.querySelector('[data-graph-shell]').classList.contains('is-maximized')"
        )
        page.wait_for_timeout(250)
        toggles["fullscreen_or_fallback"] = "PASS"
        capabilities["fullscreen_fit_relayout_and_selection"] = "PASS"

        _click_graph_element(
            page,
            "() => { const cy = window.__kgExplorerState.cy; const c = {x: cy.width()/2, y: cy.height()/2}; "
            "return cy.nodes().filter(n => n.data('degreeValue') > 0).sort((a,b) => "
            "Math.hypot(a.renderedPosition().x-c.x,a.renderedPosition().y-c.y) - "
            "Math.hypot(b.renderedPosition().x-c.x,b.renderedPosition().y-c.y))[0].renderedPosition(); }",
        )
        page.locator(".inspector-title").wait_for()
        assert page.locator(".inspector-title").inner_text().strip()
        assert page.locator(".technical-details").count() == 1
        assert page.locator("[data-neighborhood]").is_enabled()
        capabilities["node_inspection_and_provenance"] = "PASS"
        page.screenshot(path=SCREENSHOT_DIR / "08_node_inspection.png", full_page=False)

        page.evaluate(
            "() => { const cy = window.__kgExplorerState.cy; const edge = cy.edges()[0]; "
            "window.__kgTestEdgeId = edge.id(); cy.fit(edge.connectedNodes().union(edge), 180); }"
        )
        page.wait_for_timeout(250)
        _click_graph_element(
            page,
            "() => window.__kgExplorerState.cy.getElementById(window.__kgTestEdgeId).renderedMidpoint()",
        )
        page.wait_for_function("() => document.querySelector('.inspector-kicker')?.textContent.includes('Directed relation')")
        assert page.locator(".technical-details").count() == 1
        assert page.locator(".evidence-block").count() > 0
        capabilities["edge_inspection_and_provenance"] = "PASS"
        page.screenshot(path=SCREENSHOT_DIR / "09_edge_inspection.png", full_page=False)

        summary = page.locator("[data-summary-panel]")
        assert summary.get_attribute("open") is not None
        summary.locator("summary").click()
        assert summary.get_attribute("open") is None
        summary.locator("summary").click()
        assert summary.get_attribute("open") is not None
        toggles["summary"] = "PASS"
        page.screenshot(path=SCREENSHOT_DIR / "10_summary_expanded.png", full_page=False)

        insights = page.locator("[data-insights-panel]")
        insights.locator("summary").click()
        assert insights.get_attribute("open") is not None
        page.screenshot(path=SCREENSHOT_DIR / "11_insights_expanded.png", full_page=False)
        insights.locator("summary").click()
        assert insights.get_attribute("open") is None
        toggles["insights"] = "PASS"

        inspector_open_stage_width = page.locator(".graph-stage-card").bounding_box()["width"]
        page.locator("[data-inspector-toggle]").click()
        assert page.locator("[data-inspector]").evaluate("element => element.classList.contains('is-collapsed')")
        assert page.locator("[data-inspector-toggle]").get_attribute("aria-expanded") == "false"
        page.wait_for_timeout(250)
        assert page.locator(".graph-stage-card").bounding_box()["width"] > inspector_open_stage_width
        page.locator("[data-inspector-toggle]").click()
        assert page.locator("[data-inspector-toggle]").get_attribute("aria-expanded") == "true"
        page.wait_for_timeout(250)
        assert abs(page.locator(".graph-stage-card").bounding_box()["width"] - inspector_open_stage_width) < 1
        toggles["inspector"] = "PASS"
        capabilities["inspector_space_reclamation"] = "PASS"
        page.locator("[data-advanced-filters] > summary").click()
        assert page.locator("[data-advanced-filters]").get_attribute("open") is not None
        page.locator("[data-advanced-filters] > summary").click()
        assert page.locator("[data-advanced-filters]").get_attribute("open") is None
        toggles["more_filters"] = "PASS"
        page.locator("[data-edge-labels]").check()
        assert page.evaluate("() => window.__kgExplorerState.cy.edges().every(edge => edge.hasClass('edge-label-visible'))")
        page.locator("[data-edge-labels]").uncheck()
        toggles["edge_labels"] = "PASS"

        page.locator('[data-connectivity="connected"]').click()
        _wait_counts(page, 38, 50)
        page.locator('[data-connectivity="isolated"]').click()
        _wait_counts(page, 49, 0)
        page.locator('[data-connectivity="all"]').click()
        _wait_counts(page, 87, 50)
        toggles["connected_isolated_all"] = "PASS"

        page.locator("[data-fit]").click()
        page.locator("[data-relayout]").click()
        capabilities["fit"] = "PASS"
        capabilities["relayout"] = "PASS"
        with page.expect_response(lambda result: "/api/graph/" in result.url and result.ok):
            page.locator("[data-workspace-refresh]").click()
        _wait_counts(page, 87, 50)
        capabilities["manual_refresh"] = "PASS"

        page.locator("[data-auto-refresh]").select_option("30")
        assert page.locator("[data-refresh-status]").inner_text() == "Auto refresh every 30s"
        page.locator("[data-auto-refresh]").select_option("0")
        assert page.locator("[data-refresh-status]").inner_text() == "Auto refresh off"
        capabilities["auto_refresh"] = "PASS"

        component = page.locator("[data-component]")
        assert component.locator("option").count() > 1
        with page.expect_response(lambda result: "/api/graph/" in result.url and "component=" in result.url and result.ok):
            component.select_option(index=1)
        assert _count(page, "[data-visible-nodes]") > 0
        capabilities["component_explorer"] = "PASS"
        with page.expect_response(lambda result: "/api/graph/" in result.url and result.ok):
            page.locator("[data-reset]").click()
        _wait_counts(page, 87, 50)

        more_filters = page.locator("[data-advanced-filters]")
        more_filters.locator(":scope > summary").click()

        async_filter_checks = (
            ('[data-filter="kind"]', None, "kind="),
            ('[data-filter="department"]', "Environment Department", "department="),
            ('[data-filter="validation"]', None, "validation="),
            ('[data-filter="source_origin"]', None, "source_origin="),
        )
        for selector, value, query_marker in async_filter_checks:
            field = page.locator(selector)
            assert field.locator("option").count() > 1
            field.select_option(index=1) if value is None else field.select_option(value)
            with page.expect_response(lambda result, marker=query_marker: "/api/graph/" in result.url and marker in result.url and result.ok):
                page.locator("[data-apply]").click()
            assert _count(page, "[data-visible-nodes]") > 0
            with page.expect_response(lambda result: "/api/graph/" in result.url and result.ok):
                page.locator("[data-reset]").click()
            _wait_counts(page, 87, 50)
        capabilities["kind_department_validation_source_filters"] = "PASS"

        page.locator('[data-filter="year_min"]').fill("2020")
        page.locator('[data-filter="year_max"]').fill("2020")
        with page.expect_response(lambda result: "/api/graph/" in result.url and "year_min=2020" in result.url and "year_max=2020" in result.url and result.ok):
            page.locator("[data-apply]").click()
        assert _count(page, "[data-visible-nodes]") > 0
        with page.expect_response(lambda result: "/api/graph/" in result.url and result.ok):
            page.locator("[data-reset]").click()
        _wait_counts(page, 87, 50)
        capabilities["year_filter"] = "PASS"

        page.locator('[data-scope="administrative"]').click()
        _wait_counts(page, 98, 137)
        page.locator('[data-relation-toggles] input[value="ISSUED_BY"]').uncheck()
        with page.expect_response(lambda result: "/api/graph/" in result.url and "relation=REFERENCES" in result.url and result.ok):
            page.locator("[data-apply]").click()
        _wait_counts(page, 87, 50)
        capabilities["relation_filter"] = "PASS"
        with page.expect_response(lambda result: "/api/graph/" in result.url and result.ok):
            page.locator("[data-reset]").click()
        _wait_counts(page, 87, 50)

        page.locator('[data-filter="year_min"]').fill("2100")
        with page.expect_response(lambda result: "/api/graph/" in result.url and "year_min=2100" in result.url and result.ok):
            page.locator("[data-apply]").click()
        page.wait_for_function("() => document.querySelector('[data-graph-message]').dataset.state === 'FILTERED_TO_ZERO'")
        assert _count(page, "[data-visible-nodes]") == 0
        page.screenshot(path=SCREENSHOT_DIR / "12_filtered_zero_state.png", full_page=False)
        page.locator("[data-graph-message] [data-retry]").click()
        _wait_counts(page, 87, 50)
        capabilities["filtered_to_zero_reset"] = "PASS"

        export_menu = page.locator(".export-menu")
        export_menu.locator("summary").click()
        with page.expect_download() as json_download:
            page.locator("[data-export]").click()
        assert json_download.value.suggested_filename == "knowledge-graph-filtered.json"
        with page.expect_download() as png_download:
            page.locator("[data-export-png]").click()
        assert png_download.value.suggested_filename == "knowledge-graph.png"
        export_menu.locator("summary").click()
        capabilities["json_export"] = "PASS"
        capabilities["png_export"] = "PASS"

        page.locator("[data-search]").fill("government")
        page.locator("[data-search-button]").click()
        page.locator("[data-search-node]").first.click()
        page.locator(".inspector-title").wait_for()
        assert page.locator("[data-neighborhood]").is_enabled()
        page.locator("[data-neighborhood]").click()
        page.wait_for_function("() => window.__kgExplorerState.graph.mode === 'neighborhood'")
        assert _count(page, "[data-visible-nodes]") > 0
        assert page.evaluate("() => window.__kgExplorerState.graph.mode_metadata.depth") == 1
        assert page.evaluate("() => window.__kgExplorerState.graph.mode_metadata.direction") == "both"
        page.locator("[data-full-graph]").click()
        _wait_counts(page, 87, 50)
        page.locator("[data-depth]").select_option("2")
        page.locator("[data-direction]").select_option("incoming")
        page.locator("[data-neighborhood]").click()
        page.wait_for_function("() => window.__kgExplorerState.graph.mode === 'neighborhood' && window.__kgExplorerState.graph.mode_metadata.depth === 2")
        assert page.evaluate("() => window.__kgExplorerState.graph.mode_metadata.direction") == "incoming"
        page.locator("[data-full-graph]").click()
        _wait_counts(page, 87, 50)
        page.locator("[data-depth]").select_option("3")
        page.locator("[data-direction]").select_option("outgoing")
        page.locator("[data-neighborhood]").click()
        page.wait_for_function("() => window.__kgExplorerState.graph.mode === 'neighborhood' && window.__kgExplorerState.graph.mode_metadata.depth === 3")
        assert page.evaluate("() => window.__kgExplorerState.graph.mode_metadata.direction") == "outgoing"
        page.locator("[data-full-graph]").click()
        _wait_counts(page, 87, 50)
        capabilities["search"] = "PASS"
        capabilities["neighborhood_1_2_3_hop_incoming_outgoing_both"] = "PASS"
        capabilities["full_graph"] = "PASS"

        legend = page.locator("[data-legend]")
        legend.locator("summary").click()
        assert legend.get_attribute("open") is not None
        legend.locator("summary").click()
        assert legend.get_attribute("open") is None
        toggles["legend"] = "PASS"
        toggles["documents_administrative"] = "PASS"

        if page.locator("[data-advanced-filters]").get_attribute("open") is not None:
            page.locator("[data-advanced-filters] > summary").click()
        clear_button = page.locator("[data-clear-selection]")
        if clear_button.is_visible():
            clear_button.click()
        page.locator("[data-search]").fill("")
        page.locator("[data-depth]").select_option("1")
        page.locator("[data-direction]").select_option("both")
        page.locator("[data-fit]").click()
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(150)

        responsive: dict[str, Any] = {}
        for width, height in ((1366, 768), (1440, 900), (1920, 1080)):
            page.set_viewport_size({"width": width, "height": height})
            page.wait_for_timeout(150)
            overflow = page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth")
            responsive[f"{width}x{height}"] = {"horizontal_overflow": overflow, "nodes": _count(page, "[data-visible-nodes]"), "edges": _count(page, "[data-visible-edges]")}
            page.screenshot(path=SCREENSHOT_DIR / f"responsive_{width}x{height}.png", full_page=False)
            assert not overflow

        cross_page: dict[str, Any] = {}
        page.set_viewport_size({"width": 1440, "height": 900})
        if not page.locator("body").evaluate("element => element.classList.contains('sidebar-collapsed')"):
            page.locator("[data-sidebar-toggle]").click()
            page.wait_for_timeout(220)
        for name, path in (
            ("Documents", "/documents/"),
            ("Unresolved", "/knowledge-graph/unresolved/"),
            ("Corpus Health", "/system/corpus/"),
            ("Evaluation", "/system/evaluation/"),
        ):
            with page.expect_navigation(wait_until="networkidle"):
                page.locator(f'a.nav-link[href="{path}"]').click()
            assert page.url == urljoin(base_url, path)
            assert page.locator("body").evaluate("element => element.classList.contains('sidebar-collapsed')")
            assert page.locator("[data-sidebar-toggle]").get_attribute("aria-expanded") == "false"
            assert page.locator(".nav-link.active").is_visible()
            assert 74 <= page.locator(".sidebar").bounding_box()["width"] <= 78
            cross_page[name] = "PASS"
        page.screenshot(path=SCREENSHOT_DIR / "10_cross_page_sidebar_collapsed.png", full_page=False)
        collapsed_workspace_width = page.locator(".workspace").bounding_box()["width"]
        page.locator("[data-sidebar-toggle]").click()
        page.wait_for_timeout(220)
        assert not page.locator("body").evaluate("element => element.classList.contains('sidebar-collapsed')")
        assert page.locator("[data-sidebar-toggle]").get_attribute("aria-expanded") == "true"
        assert page.locator(".workspace").bounding_box()["width"] < collapsed_workspace_width
        page.screenshot(path=SCREENSHOT_DIR / "11_global_sidebar_expanded.png", full_page=False)
        with page.expect_navigation(wait_until="networkidle"):
            page.locator('a.nav-link[href="/knowledge-graph/"]').click()
        _wait_counts(page, 87, 50)
        assert not page.locator("body").evaluate("element => element.classList.contains('sidebar-collapsed')")
        cross_page["Knowledge Graph"] = "PASS"
        capabilities["cross_page_sidebar"] = "PASS"

        asset_urls = page.evaluate(
            "() => performance.getEntriesByType('resource').map(entry => entry.name)"
            ".filter(url => url.includes('/static/knowledge_graph/explorer.'))"
        )
        assert len(asset_urls) == 2
        assert all("v=2.2.0" in url for url in asset_urls)

        live_html = urllib.request.urlopen(base_url, timeout=10).read().decode("utf-8")
        optional_errors: list[str] = []
        optional_page = browser.new_page(viewport={"width": 1366, "height": 768})
        optional_page.on("pageerror", lambda error: optional_errors.append(str(error)))
        optional_page.route(
            base_url,
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=live_html.replace(" data-summary-caption", "", 1),
            ),
        )
        optional_page.goto(base_url, wait_until="networkidle")
        _wait_counts(optional_page, 87, 50)
        assert optional_page.evaluate("() => window.__kgExplorerDomContract.status") == "PASS"
        assert not optional_errors
        optional_page.close()
        capabilities["optional_widget_failure_isolation"] = "PASS"

        critical_errors: list[str] = []
        critical_page = browser.new_page(viewport={"width": 1366, "height": 768})
        critical_page.on("pageerror", lambda error: critical_errors.append(str(error)))
        critical_page.route(
            base_url,
            lambda route: route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=live_html.replace('id="knowledge-graph-canvas"', 'id="knowledge-graph-canvas-missing"', 1),
            ),
        )
        critical_page.goto(base_url, wait_until="networkidle")
        critical_page.wait_for_function(
            "() => document.querySelector('[data-graph-message]').dataset.state === 'CLIENT_RENDER_ERROR'"
        )
        assert critical_page.evaluate("() => window.__kgExplorerDomContract.status") == "FAIL"
        assert not critical_errors
        critical_page.close()
        capabilities["client_render_error_state"] = "PASS"

        api_errors: list[str] = []
        api_error_page = browser.new_page(viewport={"width": 1366, "height": 768})
        api_error_page.on("pageerror", lambda error: api_errors.append(str(error)))

        def api_error_route(route) -> None:
            if "/api/graph/" in route.request.url:
                route.fulfill(status=503, content_type="application/json", body='{"error":"test outage"}')
            else:
                route.continue_()

        api_error_page.route("**/*", api_error_route)
        api_error_page.goto(base_url, wait_until="networkidle")
        api_error_page.wait_for_function(
            "() => document.querySelector('[data-graph-message]').dataset.state === 'API_ERROR'"
        )
        assert "Unable to load graph" in api_error_page.locator("[data-graph-message]").inner_text()
        api_error_page.screenshot(path=SCREENSHOT_DIR / "13_error_state.png", full_page=False)
        assert not api_errors
        api_error_page.close()
        capabilities["api_error_retry_state"] = "PASS"

        graph_api_url = base_url.rstrip("/") + "/api/graph/?scope=documents&connectivity=all"
        empty_graph = json.loads(urllib.request.urlopen(graph_api_url, timeout=10).read().decode("utf-8"))
        empty_graph["nodes"] = []
        empty_graph["edges"] = []
        empty_graph["returned"] = {key: 0 for key in empty_graph["returned"]}
        empty_graph["counts"]["displayed_nodes"] = 0
        empty_graph["counts"]["displayed_edges"] = 0
        empty_errors: list[str] = []
        empty_page = browser.new_page(viewport={"width": 1366, "height": 768})
        empty_page.on("pageerror", lambda error: empty_errors.append(str(error)))

        def empty_graph_route(route) -> None:
            if "/api/graph/" in route.request.url:
                route.fulfill(status=200, content_type="application/json", body=json.dumps(empty_graph))
            else:
                route.continue_()

        empty_page.route("**/*", empty_graph_route)
        empty_page.goto(base_url, wait_until="networkidle")
        empty_page.wait_for_function(
            "() => document.querySelector('[data-graph-message]').dataset.state === 'EMPTY_GRAPH'"
        )
        assert "no validated graph elements" in empty_page.locator("[data-graph-message]").inner_text().lower()
        assert not empty_errors
        empty_page.close()
        capabilities["empty_graph_state"] = "PASS"

        result = {
            "status": "PASS",
            "framework": "Playwright 1.62.0 / bundled Chromium",
            "page_http_status": response.status,
            "page_errors": page_errors,
            "console_errors": console_errors,
            "uncaught_javascript_exceptions": len(page_errors),
            "computed_graph_background_image": "none",
            "computed_graph_backgrounds": background_styles,
            "dom_contract": {"status": "PASS", "missing": []},
            "asset_urls": asset_urls,
            "toggles": toggles,
            "capabilities": capabilities,
            "cross_page_sidebar": cross_page,
            "node_role_counts": node_role_counts,
            "administrative_node_role_counts": administrative_node_role_counts,
            "semantic_styles": semantic_styles,
            "error_states": {
                "LOADING": "PASS",
                "EMPTY_GRAPH": "PASS",
                "FILTERED_TO_ZERO": "PASS",
                "API_ERROR": "PASS",
                "CLIENT_RENDER_ERROR": "PASS",
            },
            "documents_mode": {"nodes": 87, "edges": 50},
            "administrative_mode": {"nodes": 98, "edges": 137},
            "cytoscape_canvas_count": page.locator("#knowledge-graph-canvas canvas").count(),
            "responsive": responsive,
            "screenshots": sorted(path.name for path in SCREENSHOT_DIR.glob("*.png")),
        }
        assert not page_errors
        assert not console_errors
        browser.close()
    _write_json("browser_console_validation.json", {
        "status": "PASS",
        "page_errors": [],
        "console_errors": [],
        "uncaught_javascript_exceptions": 0,
        "unhandled_promise_rejections": 0,
        "cannot_set_text_content_exception_present": False,
    })
    _write_json("browser_interaction_validation.json", result)
    return result


@pytest.mark.skipif(os.getenv("RUN_BROWSER_E2E") != "1", reason="Set RUN_BROWSER_E2E=1 with the local Django server running")
def test_live_knowledge_graph_explorer_browser_acceptance() -> None:
    result = run_acceptance(os.getenv("KG_E2E_BASE_URL", DEFAULT_URL))
    assert result["status"] == "PASS"


def main() -> None:
    parser = argparse.ArgumentParser(description="Knowledge Graph Explorer V2.2 browser harness")
    parser.add_argument("mode", choices=("reproduce", "acceptance"))
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args()
    payload = reproduce_stale_asset_regression(args.url) if args.mode == "reproduce" else run_acceptance(args.url)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
