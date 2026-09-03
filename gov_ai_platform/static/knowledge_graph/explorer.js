(function () {
  "use strict";

  const root = document.querySelector("[data-graph-explorer]");
  if (!root) return;
  document.body.classList.add("kg-explorer-page", "context-collapsed");

  const q = (selector, scope = root) => scope.querySelector(selector);
  const qa = (selector, scope = root) => Array.from(scope.querySelectorAll(selector));
  const readJson = (id) => {
    const element = document.getElementById(id);
    if (!element) throw new Error(`Missing required JSON element #${id}`);
    return JSON.parse(element.textContent);
  };
  const api = readJson("kg-explorer-api");
  const initialSummary = readJson("kg-explorer-initial-summary");
  const shell = q("[data-graph-shell]");
  const filterPanel = q("[data-filter-panel]");
  const inspector = q("[data-inspector]");
  const detailPanel = q("[data-graph-detail]");
  const canvas = q("#knowledge-graph-canvas");
  const message = q("[data-graph-message]");
  const tooltip = q("[data-tooltip]");

  const state = {
    cy: null,
    summary: initialSummary,
    graph: null,
    mode: "full",
    graphScope: "documents",
    layout: "auto",
    direction: "both",
    depth: 1,
    connectivity: "all",
    componentId: "",
    sidebarCollapsed: document.body.classList.contains("sidebar-collapsed"),
    filtersCollapsed: false,
    summaryCollapsed: false,
    insightsCollapsed: true,
    inspectorCollapsed: false,
    legendOpen: false,
    advancedFiltersOpen: false,
    focusMode: false,
    fullscreen: false,
    fullscreenFallback: false,
    edgeLabels: false,
    activeFilters: 0,
    selectedId: null,
    selectedType: null,
    timer: null,
    refreshSeconds: 0,
    requestSerial: 0,
    phase: "BOOTSTRAP",
    diagnostics: [],
  };
  window.__kgExplorerState = state;

  const graphRoleLabels = Object.freeze({
    ISOLATED_DOCUMENT: "Isolated document",
    REFERENCED_DOCUMENT: "Referenced document",
    CITING_DOCUMENT: "Citing document",
    REFERENCED_AND_CITING_DOCUMENT: "Referenced + citing document",
    DEPARTMENT: "Department",
  });
  window.__kgNodeRoleContract = Object.freeze({
    source: "validated REFERENCES only",
    candidateNodesIncluded: false,
    roles: Object.keys(graphRoleLabels),
  });

  const esc = (value) => String(value == null ? "" : value).replace(/[&<>'"]/g, (character) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"}[character]));
  const pretty = (value) => String(value || "Unknown").replaceAll("_", " ").toLowerCase().replace(/\b\w/g, (letter) => letter.toUpperCase());
  const graphRoleLabel = (value) => graphRoleLabels[value] || pretty(value);
  const formatDate = (value) => value ? new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: value.includes("T") ? "short" : undefined}).format(new Date(value)) : "—";
  const nodeById = (id) => state.graph && state.graph.nodes.find((node) => node.id === id);

  function diagnostic(phase, detail = {}) {
    state.phase = phase;
    state.diagnostics.push({phase, at: new Date().toISOString(), ...detail});
    if (location.hostname === "127.0.0.1" || location.hostname === "localhost") {
      console.debug(`[KG Explorer] ${phase}`, detail);
    }
  }

  function validateDomContract() {
    const contract = {
      "#knowledge-graph-canvas": canvas,
      "[data-visible-nodes]": q("[data-visible-nodes]"),
      "[data-visible-edges]": q("[data-visible-edges]"),
      "[data-graph-message]": message,
      "[data-graph-shell]": shell,
    };
    const missing = Object.entries(contract).filter(([, element]) => !element).map(([selector]) => selector);
    window.__kgExplorerDomContract = {status: missing.length ? "FAIL" : "PASS", missing};
    return missing;
  }

  function announce(text, error = false, action = "", phase = error ? "API_ERROR" : text ? "LOADING" : "READY") {
    if (!message) return;
    message.dataset.state = phase;
    message.classList.toggle("is-error", error);
    message.innerHTML = text ? `${esc(text)}${action ? `<button class="button" type="button" data-retry>${esc(action)}</button>` : ""}` : "";
    const retry = q("[data-retry]", message);
    if (retry) retry.addEventListener("click", () => action === "Reset filters" ? resetFilters() : refreshCurrent({forceLayout: true}));
  }

  async function requestJson(url) {
    let response;
    try {
      response = await fetch(url, {headers: {Accept: "application/json"}, cache: "no-store"});
    } catch (error) {
      throw new Error("Graph API unavailable. Check that the Django server is running.");
    }
    if (!response.ok) {
      let payload = {};
      try { payload = await response.json(); } catch (_) { /* response was not JSON */ }
      const detail = payload.error || `HTTP ${response.status}`;
      throw new Error(response.status >= 500 ? `Database or graph service unavailable: ${detail}` : detail);
    }
    return response.json();
  }

  function graphParams() {
    const params = new URLSearchParams({scope: state.graphScope, connectivity: state.connectivity});
    qa("[data-relation-toggles] input:checked").forEach((input) => params.append("relation", input.value));
    qa("[data-filter]").forEach((field) => { if (field.value && field.value !== "any") params.set(field.dataset.filter, field.value); });
    if (state.componentId) params.set("component", state.componentId);
    return params;
  }

  function fillOptions(selector, values, formatter = pretty) {
    const field = q(selector);
    if (!field) return;
    const current = field.value;
    Array.from(field.options).slice(1).forEach((option) => option.remove());
    values.forEach((value) => field.add(new Option(formatter(value), value)));
    field.value = values.includes(current) ? current : "";
  }

  function updateSummary(summary) {
    state.summary = summary;
    qa("[data-metric]").forEach((element) => {
      const value = summary[element.dataset.metric];
      if (value !== undefined) element.textContent = Number.isInteger(value) ? value.toLocaleString() : value;
    });
    qa("[data-relation-count]").forEach((element) => { element.textContent = (summary.relation_counts[element.dataset.relationCount] || 0).toLocaleString(); });
    qa("[data-coverage]").forEach((element) => { element.textContent = `${(summary.graph_coverage * 100).toFixed(1)}%`; });
    qa("[data-coverage-long]").forEach((element) => { element.textContent = `${(summary.graph_coverage * 100).toFixed(2)}%`; });
    const caption = q("[data-summary-caption]");
    if (caption) caption.textContent = `${summary.total_documents} documents · ${summary.references} references · ${summary.connected_documents} connected · ${(summary.graph_coverage * 100).toFixed(1)}% coverage · ${summary.components} components`;
    fillOptions('[data-filter="kind"]', summary.filter_options.document_kinds);
    fillOptions('[data-filter="department"]', summary.filter_options.departments, (value) => value);
    fillOptions('[data-filter="validation"]', summary.filter_options.validation_states, pretty);
    fillOptions('[data-filter="source_origin"]', summary.filter_options.source_origins, pretty);
    renderRanking("[data-top-cited]", summary.top_cited);
    renderRanking("[data-top-citing]", summary.top_citing);
    renderRanking("[data-most-connected]", summary.most_connected);
    const departments = q("[data-department-counts]");
    if (departments) departments.innerHTML = summary.department_document_counts.slice(0, 7).map((item) => `<li>${esc(item.department)} <b>${item.count}</b></li>`).join("");
    const liveStatus = q("[data-live-status]");
    if (liveStatus) liveStatus.classList.remove("is-error");
    const liveLabel = q("[data-live-label]");
    if (liveLabel) liveLabel.textContent = "Live";
  }

  function renderRanking(selector, items) {
    const list = q(selector);
    if (list) list.innerHTML = items.map((item) => `<li><button type="button" data-rank-document="${item.document_id}" title="${esc(item.subject)}">${esc(item.label)}</button> <b>${item.count}</b></li>`).join("");
  }

  function updateComponents(payload) {
    const field = q("[data-component]");
    if (!field) return;
    field.innerHTML = '<option value="">All components</option>';
    payload.items.forEach((component, index) => {
      const suffix = component.isolated ? "isolated" : `${component.relation_count} relations`;
      field.add(new Option(`${index === 0 ? "Largest" : `Component ${index + 1}`} · ${component.node_count} nodes · ${suffix}`, component.component_id));
    });
    field.value = state.componentId;
  }

  function cyElements(graph) {
    const nodes = graph.nodes.map((node) => {
      const role = node.graph_role || node.type;
      const referenceDegree = node.reference_degree || {incoming: 0, outgoing: 0, degree: 0};
      return {
        data: {
          ...node,
          ...node.metadata,
          label: node.label,
          degreeValue: node.degree.degree,
          graphRole: role,
          incomingReferences: referenceDegree.incoming || 0,
          outgoingReferences: referenceDegree.outgoing || 0,
          totalReferenceDegree: referenceDegree.degree || 0,
        },
        classes: `${node.type.toLowerCase()} role-${role.toLowerCase().replaceAll("_", "-")}`,
      };
    });
    const edges = graph.edges.map((edge) => ({data: {...edge, label: pretty(edge.relation_type)}, classes: edge.relation_type.toLowerCase()}));
    return [...nodes, ...edges];
  }

  const cyStyles = [
    {selector: "node", style: {"background-color": "#dbe7ff", "border-width": 2, "border-color": "#2f6fed", shape: "round-rectangle", width: "mapData(degreeValue,0,8,34,61)", height: "mapData(degreeValue,0,8,24,39)", label: "data(label)", color: "#17324d", "font-size": 10, "font-weight": 600, "text-wrap": "ellipsis", "text-max-width": 126, "text-valign": "bottom", "text-margin-y": 8, "text-background-color": "#f7f9fc", "text-background-opacity": .88, "text-background-padding": 2, "overlay-opacity": 0, "transition-property": "opacity,border-width,background-color", "transition-duration": "130ms"}},
    {selector: "node.role-referenced-document", style: {shape: "round-rectangle", "background-color": "#dbe7ff", "border-color": "#2f6fed", "border-style": "solid"}},
    {selector: "node.role-citing-document", style: {shape: "hexagon", "background-color": "#fff0d6", "border-color": "#b56a1e", "border-style": "solid"}},
    {selector: "node.role-referenced-and-citing-document", style: {shape: "octagon", "background-color": "#eee7ff", "border-color": "#6c4fc2", "border-style": "double", "border-width": 4}},
    {selector: "node.role-isolated-document", style: {shape: "ellipse", "border-style": "dashed", "background-color": "#f1f4f7", "border-color": "#8292a4"}},
    {selector: "node.department", style: {shape: "diamond", "background-color": "#dff4ee", "border-color": "#0f8b8d", "border-style": "solid", width: 40, height: 40}},
    {selector: "edge", style: {width: 1.7, "line-color": "#6f8fd7", "target-arrow-color": "#6f8fd7", "target-arrow-shape": "triangle", "curve-style": "bezier", "arrow-scale": .8, opacity: .72, label: "", "font-size": 8, color: "#53677c", "text-background-color": "#fff", "text-background-opacity": .9, "text-background-padding": 2, "transition-property": "opacity,width", "transition-duration": "130ms"}},
    {selector: "edge.issued_by", style: {"line-color": "#44a79d", "target-arrow-color": "#44a79d", "line-style": "dashed"}},
    {selector: "edge.amends,edge.supersedes,edge.partially_supersedes,edge.cancels,edge.corrects,edge.extends", style: {"line-color": "#8b73b6", "target-arrow-color": "#8b73b6"}},
    {selector: ".edge-label-visible", style: {label: "data(label)"}},
    {selector: ".label-hidden", style: {label: ""}},
    {selector: ".dimmed", style: {opacity: .22}},
    {selector: ".related", style: {opacity: 1, "z-index": 8}},
    {selector: "node.selected-emphasis", style: {"border-width": 5, "border-color": "#f0a52b", "underlay-color": "#f0a52b", "underlay-opacity": .22, "underlay-padding": 7, "z-index": 12}},
    {selector: "edge.selected-emphasis", style: {width: 4, opacity: 1, "z-index": 12}},
  ];

  function layoutOptions() {
    if (state.layout === "hierarchy") return {name: "breadthfirst", directed: true, padding: 34, spacingFactor: 1.12, animate: false};
    if (state.layout === "force") return {name: "cose", animate: false, randomize: true, padding: 34, nodeRepulsion: () => 16000, idealEdgeLength: () => 120, componentSpacing: 110, gravity: .18, numIter: 1400};
    if (state.mode === "neighborhood") return {name: "concentric", padding: 38, minNodeSpacing: 60, animate: false, concentric: (node) => node.data("degreeValue"), levelWidth: () => 2};
    return {name: "cose", animate: false, randomize: true, padding: 34, nodeRepulsion: () => 10500, idealEdgeLength: () => 95, componentSpacing: 75, gravity: .28, numIter: 1000};
  }

  function relayout(fit = true) {
    if (!state.cy || !state.cy.nodes().length) return;
    const options = layoutOptions();
    options.fit = fit;
    state.cy.layout(options).run();
  }

  function syncZoomLabels() {
    if (!state.cy) return;
    const hide = state.cy.zoom() < .68;
    state.cy.nodes().toggleClass("label-hidden", hide);
  }

  function buildGraph(graph, {forceLayout = false} = {}) {
    const previousFingerprint = state.graph && state.graph.graph_fingerprint;
    const sameTopology = state.graph && previousFingerprint === graph.graph_fingerprint && state.graph.nodes.length === graph.nodes.length && state.graph.edges.length === graph.edges.length;
    const viewport = state.cy ? {zoom: state.cy.zoom(), pan: state.cy.pan()} : null;
    const positions = {};
    if (state.cy) state.cy.nodes().forEach((node) => { positions[node.id()] = node.position(); });
    if (state.cy) state.cy.destroy();
    state.graph = graph;
    state.mode = graph.mode;
    state.cy = cytoscape({container: canvas, elements: cyElements(graph), style: cyStyles, minZoom: .16, maxZoom: 3.2, wheelSensitivity: .18, desktopTapThreshold: 8, boxSelectionEnabled: true});
    if (sameTopology && !forceLayout && Object.keys(positions).length) {
      state.cy.nodes().positions((node) => positions[node.id()] || {x: 0, y: 0});
      state.cy.zoom(viewport.zoom); state.cy.pan(viewport.pan);
    } else relayout(true);
    state.cy.on("zoom", syncZoomLabels);
    state.cy.on("tap", "node", (event) => selectElement(event.target, "node"));
    state.cy.on("tap", "edge", (event) => selectElement(event.target, "edge"));
    state.cy.on("tap", (event) => { if (event.target === state.cy) clearSelection(); });
    if (tooltip) {
      state.cy.on("mouseover", "node,edge", showTooltip);
      state.cy.on("mouseout", "node,edge", () => { tooltip.hidden = true; });
    }
    syncZoomLabels();
    updateEdgeLabels();
    updateGraphStatus();
    if (state.selectedId) {
      const selected = state.cy.getElementById(state.selectedId);
      if (selected.length) emphasize(selected); else removedSelectionState();
    }
  }

  function updateEdgeLabels() {
    if (!state.cy) return;
    state.cy.edges().toggleClass("edge-label-visible", state.edgeLabels || state.mode === "neighborhood");
  }

  function updateGraphStatus() {
    const graph = state.graph;
    if (!graph) return;
    q("[data-visible-nodes]").textContent = graph.counts.displayed_nodes;
    q("[data-visible-edges]").textContent = graph.counts.displayed_edges;
    const totalNodes = state.graphScope === "documents" ? graph.counts.total_documents : graph.counts.total_graph_nodes;
    const totalEdges = state.graphScope === "documents" ? graph.counts.document_relations : graph.counts.total_validated_edges;
    const displayStatus = q("[data-display-status]");
    if (displayStatus) displayStatus.textContent = `Displayed ${graph.counts.displayed_nodes} of ${totalNodes} nodes · ${graph.counts.displayed_edges} of ${totalEdges} edges`;
    const scope = state.graphScope === "documents" ? "Documents" : "Administrative";
    const component = state.componentId ? "selected component" : state.connectivity === "all" ? "all components" : `${state.connectivity} documents`;
    const modeLabel = q("[data-mode-label]");
    if (modeLabel) modeLabel.textContent = graph.mode === "neighborhood" ? `${scope} · ${graph.mode_metadata.depth}-hop neighborhood` : `${scope} · ${component}`;
    const truncation = q("[data-truncation]");
    if (truncation) {
      truncation.hidden = !graph.truncated;
      truncation.textContent = graph.truncated ? `Partial graph: ${graph.truncation_reason}. Increase response bounds to show more.` : "";
    }
    if (graph.nodes.length) announce("", false, "", "READY");
    else if (state.activeFilters) announce("No graph elements match these filters.", false, "Reset filters", "FILTERED_TO_ZERO");
    else announce("The database contains no validated graph elements.", false, "", "EMPTY_GRAPH");
  }

  function syncNeighborhoodAction() {
    const documentSelected = state.selectedType === "node" && state.cy && state.selectedId
      && state.cy.getElementById(state.selectedId).data("type") === "DOCUMENT";
    qa("[data-neighborhood]").forEach((button) => {
      button.disabled = !documentSelected;
      button.title = documentSelected ? "Show the selected document's neighborhood" : "Select a document to enable neighborhood view";
    });
  }

  async function loadGraph({forceLayout = false} = {}) {
    const serial = ++state.requestSerial;
    diagnostic("LOADING", {mode: state.graphScope});
    announce("Loading graph…", false, "", "LOADING");
    qa("[data-refresh], [data-workspace-refresh]").forEach((button) => { button.disabled = true; });
    let graph;
    try {
      graph = await requestJson(`${api.graph}?${graphParams()}`);
      diagnostic("API_FETCH_SUCCESS", {nodes: graph.nodes.length, edges: graph.edges.length});
      diagnostic("JSON_PARSE_SUCCESS", {fingerprint: graph.graph_fingerprint});
    } catch (error) {
      diagnostic("API_ERROR", {message: error.message});
      announce(`Unable to load graph. ${error.message}`, true, "Retry", "API_ERROR");
      const liveStatus = q("[data-live-status]");
      if (liveStatus) liveStatus.classList.add("is-error");
      const liveLabel = q("[data-live-label]");
      if (liveLabel) liveLabel.textContent = "Unavailable";
      return;
    } finally {
      if (!graph) qa("[data-refresh], [data-workspace-refresh]").forEach((button) => { button.disabled = false; });
    }
    if (serial !== state.requestSerial) return;
    try {
      diagnostic("NODE_CONVERSION_SUCCESS", {nodes: graph.nodes.length});
      diagnostic("EDGE_CONVERSION_SUCCESS", {edges: graph.edges.length});
      buildGraph(graph, {forceLayout});
      diagnostic("CYTOSCAPE_RENDER_SUCCESS", {nodes: state.cy.nodes().length, edges: state.cy.edges().length});
    } catch (error) {
      diagnostic("CLIENT_RENDER_ERROR", {message: error.message});
      announce(`Unable to render graph. ${error.message}`, true, "Retry", "CLIENT_RENDER_ERROR");
      qa("[data-refresh], [data-workspace-refresh]").forEach((button) => { button.disabled = false; });
      return;
    }
    try { updateSummary(graph.summary); }
    catch (error) { diagnostic("OPTIONAL_SUMMARY_ERROR", {message: error.message}); }
    const liveStatus = q("[data-live-status]");
    if (liveStatus) liveStatus.title = `Updated ${formatDate(graph.generated_at)}`;
    qa("[data-refresh], [data-workspace-refresh]").forEach((button) => { button.disabled = false; });
  }

  function showTooltip(event) {
    const item = event.target;
    const data = item.data();
    const source = data.source ? nodeById(data.source) : null;
    const target = data.target ? nodeById(data.target) : null;
    tooltip.innerHTML = item.isNode()
      ? `<strong>${esc(data.label)}</strong><br>Type: ${esc(pretty(data.document_kind || data.type))}<br>Graph role: ${esc(graphRoleLabel(data.graphRole))}${data.type === "DOCUMENT" ? `<br>Incoming references: ${data.incomingReferences}<br>Outgoing references: ${data.outgoingReferences}` : ""}${data.department ? `<br>Department: ${esc(data.department)}` : ""}`
      : `<strong>${esc(pretty(data.relation_type))}</strong><br>${esc(source ? source.label : data.source)} → ${esc(target ? target.label : data.target)}<br>${esc(data.validation_state)}${data.provenance_summary && data.provenance_summary.source_page ? ` · page ${data.provenance_summary.source_page}` : ""}`;
    const box = canvas.getBoundingClientRect();
    tooltip.style.left = `${Math.min(event.renderedPosition.x + 14, box.width - 295)}px`;
    tooltip.style.top = `${Math.max(62, event.renderedPosition.y - 4)}px`;
    tooltip.hidden = false;
  }

  function emphasize(element) {
    state.cy.elements().removeClass("dimmed related selected-emphasis");
    state.cy.elements().addClass("dimmed");
    if (element.isNode()) {
      const neighborhood = element.closedNeighborhood();
      neighborhood.removeClass("dimmed").addClass("related");
    } else {
      element.add(element.source()).add(element.target()).removeClass("dimmed").addClass("related");
    }
    element.addClass("selected-emphasis");
    const clear = q("[data-clear-selection]");
    if (clear) clear.hidden = false;
  }

  async function selectElement(element, type) {
    state.selectedId = element.id(); state.selectedType = type;
    syncNeighborhoodAction();
    emphasize(element);
    if (inspector && shell) {
      inspector.classList.remove("is-collapsed"); shell.classList.remove("inspector-collapsed"); state.inspectorCollapsed = false;
      qa("[data-inspector-toggle]").forEach((button) => button.setAttribute("aria-expanded", "true"));
    }
    if (!detailPanel) return;
    detailPanel.innerHTML = '<p class="muted">Loading selection…</p>';
    try {
      const url = type === "node" ? api.node_template.replace("node-placeholder", encodeURIComponent(element.id())) : api.edge_template.replace("0", element.data("relation_id"));
      const detail = await requestJson(url);
      detailPanel.innerHTML = type === "node" ? renderNodeDetail(detail, element.data()) : renderEdgeDetail(detail);
      bindInspectorActions();
    } catch (error) { detailPanel.innerHTML = `<div class="empty-inspector"><h3>Details unavailable</h3><p>${esc(error.message)}</p></div>`; }
  }

  function facts(items) {
    return `<dl class="inspector-facts">${items.map(([name, value]) => `<div><dt>${esc(name)}</dt><dd>${esc(value == null || value === "" ? "—" : value)}</dd></div>`).join("")}</dl>`;
  }

  function technical(items) {
    return `<details class="technical-details"><summary>Technical provenance</summary><dl class="technical-grid">${items.map(([name, value]) => `<div><dt>${esc(name)}</dt><dd>${esc(value == null || value === "" ? "—" : value)}</dd></div>`).join("")}</dl></details>`;
  }

  function renderNodeDetail(detail, graphData) {
    if (detail.type === "DEPARTMENT") {
      const item = detail.department;
      const aliases = [...new Set(item.aliases || [])];
      return `<span class="inspector-kicker">Department</span><h3 class="inspector-title">${esc(item.canonical_name)}</h3><p>${item.document_count} validated issued documents</p>${facts([["Graph role", "Department"], ["Aliases", aliases.join(", ") || "None"], ["Documents", item.document_count]])}${technical([["Department ID", item.id], ["Normalized name", item.normalized_name]])}`;
    }
    const item = detail.document;
    const title = graphData.label || item.gr_number || item.subject || item.file_name;
    const referenceDegree = detail.reference_degree || {
      incoming: graphData.incomingReferences || 0,
      outgoing: graphData.outgoingReferences || 0,
      degree: graphData.totalReferenceDegree || 0,
    };
    return `<span class="inspector-kicker">${esc(pretty(item.document_kind || item.document_type))}</span><h3 class="inspector-title">${esc(title)}</h3><p class="inspector-subject">${esc(item.subject || "No extracted subject")}</p><div class="inspector-actions"><a href="/documents/${item.id}/pdf/" target="_blank" rel="noopener">Open PDF</a><a href="/documents/${item.id}/">Document page</a><button type="button" data-focus-neighborhood="${item.id}">Focus neighborhood</button></div>${facts([["Graph role", graphRoleLabel(detail.graph_role || graphData.graphRole)], ["Incoming REFERENCES", referenceDegree.incoming], ["Outgoing REFERENCES", referenceDegree.outgoing], ["Total REFERENCES degree", referenceDegree.degree], ["Date", item.gr_date || "—"], ["Department", item.department || "—"], ["Source", pretty(item.source_origin)], ["Pages", item.page_count]])}${technical([["Canonical identity", item.canonical_identity], ["File", item.file_name], ["Local path", item.file_path], ["SHA-256", item.sha256], ["Extraction", item.extraction_method], ["Chunks", item.chunk_count]])}`;
  }

  function renderEdgeDetail(detail) {
    const target = detail.target_number || detail.target_department || `Department ${detail.target_department_id}`;
    const evidence = (detail.evidence || []).map((item) => `<div class="evidence-block"><strong>Page ${esc(item.source_page || "—")}</strong><p>${esc(item.raw_evidence_text || "Stored relation evidence")}</p></div>`).join("");
    return `<span class="inspector-kicker">Directed relation</span><h3 class="inspector-title">${esc(pretty(detail.relation_type))}</h3><p class="inspector-subject"><strong>${esc(detail.source_number || detail.source_file)}</strong> → <strong>${esc(target)}</strong></p>${facts([["Validation", pretty(detail.validation_status)], ["Evidence items", (detail.evidence || []).length], ["Direction", "Source → target"], ["Provenance", detail.provenance_complete ? "Complete" : "Unavailable"]])}${evidence || '<p class="muted">Issuer relation; no document citation excerpt is required.</p>'}${technical([["Relation ID", detail.id], ["Candidate ID", detail.created_from_candidate_id], ["Source document", detail.source_document_id], ["Target document", detail.target_document_id], ["Target department", detail.target_department_id]])}`;
  }

  function bindInspectorActions() {
    const button = q("[data-focus-neighborhood]", detailPanel);
    if (button) button.addEventListener("click", () => loadNeighborhood(Number(button.dataset.focusNeighborhood)));
  }

  async function loadNeighborhood(documentId, {forceLayout = true} = {}) {
    announce("Loading neighborhood…");
    try {
      const params = graphParams(); params.set("depth", state.depth); params.set("direction", state.direction);
      const url = api.neighborhood_template.replace("0", documentId);
      const graph = await requestJson(`${url}?${params}`);
      buildGraph(graph, {forceLayout});
      try { updateSummary(graph.summary); } catch (error) { diagnostic("OPTIONAL_SUMMARY_ERROR", {message: error.message}); }
    } catch (error) { announce(`Unable to load neighborhood. ${error.message}`, true, "Retry", "API_ERROR"); }
  }

  function refreshCurrent(options = {}) {
    const rootDocumentId = state.graph && state.graph.mode === "neighborhood" && state.graph.mode_metadata.root_document_id;
    return rootDocumentId ? loadNeighborhood(Number(rootDocumentId), options) : loadGraph(options);
  }

  function clearSelection() {
    state.selectedId = null; state.selectedType = null;
    syncNeighborhoodAction();
    if (tooltip) tooltip.hidden = true;
    if (state.cy) state.cy.elements().removeClass("dimmed related selected-emphasis");
    const clear = q("[data-clear-selection]");
    if (clear) clear.hidden = true;
    if (detailPanel) detailPanel.innerHTML = '<div class="empty-inspector"><span class="empty-icon">◎</span><h3>Select a node or edge</h3><p>See useful metadata, relations, and stored provenance. Nothing here changes the graph.</p></div>';
  }

  function removedSelectionState() {
    state.selectedId = null; state.selectedType = null;
    syncNeighborhoodAction();
    const clear = q("[data-clear-selection]");
    if (clear) clear.hidden = true;
    if (detailPanel) detailPanel.innerHTML = '<div class="empty-inspector"><h3>Selection removed</h3><p>The selected item is no longer visible under the current filters. Clear filters or choose another node.</p></div>';
  }

  function updateActiveFilterCount() {
    const relationInputs = qa("[data-relation-toggles] input");
    let count = relationInputs.filter((input) => !input.checked).length;
    qa("[data-filter]").forEach((field) => {
      const defaults = {presence: "any", max_nodes: "500", max_edges: "2000"};
      const defaultValue = defaults[field.dataset.filter] || "";
      if (field.value && field.value !== defaultValue) count += 1;
    });
    if (state.componentId) count += 1;
    if (state.connectivity !== "all") count += 1;
    if (state.graphScope !== "documents") count += 1;
    state.activeFilters = count;
    const badge = q("[data-filter-badge]");
    if (badge) { badge.textContent = count; badge.hidden = count === 0; }
    qa("[data-active-filter-count]").forEach((item) => { item.textContent = count; item.hidden = count === 0; });
  }

  function setScope(scope, reload = true) {
    state.graphScope = scope;
    qa("[data-scope]").forEach((button) => button.classList.toggle("is-active", button.dataset.scope === scope));
    const hint = q("[data-scope-hint]");
    if (hint) hint.textContent = scope === "documents" ? "Document relationships only" : "Documents, active issuers, and all validated edges";
    updateActiveFilterCount();
    if (reload) loadGraph({forceLayout: true});
  }

  function setConnectivity(value, reload = true) {
    state.connectivity = value;
    qa("[data-connectivity]").forEach((button) => button.classList.toggle("is-active", button.dataset.connectivity === value));
    updateActiveFilterCount();
    if (reload) loadGraph({forceLayout: true});
  }

  function resetFilters() {
    qa("[data-relation-toggles] input").forEach((input) => { input.checked = true; });
    qa("[data-filter]").forEach((field) => { field.value = field.dataset.filter === "presence" ? "any" : field.dataset.filter.startsWith("max_") ? (field.dataset.filter === "max_nodes" ? "500" : "2000") : ""; });
    state.componentId = "";
    const component = q("[data-component]");
    if (component) component.value = "";
    setScope("documents", false); setConnectivity("all", false); clearSelection(); updateActiveFilterCount(); loadGraph({forceLayout: true});
  }

  function togglePanel(kind) {
    const panel = kind === "filters" ? filterPanel : inspector;
    if (!panel || !shell) return;
    const className = kind === "filters" ? "filter-collapsed" : "inspector-collapsed";
    const key = kind === "filters" ? "filtersCollapsed" : "inspectorCollapsed";
    state[key] = !state[key]; panel.classList.toggle("is-collapsed", state[key]); shell.classList.toggle(className, state[key]);
    qa(kind === "filters" ? "[data-filter-toggle]" : "[data-inspector-toggle]").forEach((button) => {
      button.setAttribute("aria-expanded", String(!state[key]));
    });
    window.setTimeout(() => { if (state.cy) { state.cy.resize(); state.cy.fit(undefined, 34); } }, 190);
  }

  function toggleFocus(force) {
    state.focusMode = force === undefined ? !state.focusMode : force;
    root.classList.toggle("is-focus-mode", state.focusMode);
    document.body.classList.toggle("kg-focus-mode", state.focusMode);
    qa("[data-focus-mode], [data-workspace-focus]").forEach((button) => {
      button.textContent = state.focusMode ? "Exit focus" : "Focus";
      button.setAttribute("aria-pressed", String(state.focusMode));
    });
    window.setTimeout(() => { if (state.cy) { state.cy.resize(); state.cy.fit(undefined, 36); } }, 80);
  }

  async function toggleFullscreen() {
    if (!shell) return;
    if (document.fullscreenElement) { await document.exitFullscreen(); return; }
    try { await shell.requestFullscreen(); state.fullscreenFallback = false; }
    catch (_) { state.fullscreenFallback = !state.fullscreenFallback; shell.classList.toggle("is-maximized", state.fullscreenFallback); }
    state.fullscreen = Boolean(document.fullscreenElement || state.fullscreenFallback);
    qa("[data-fullscreen], [data-workspace-fullscreen]").forEach((button) => {
      button.textContent = state.fullscreen ? "Exit fullscreen" : "Fullscreen";
      button.setAttribute("aria-pressed", String(state.fullscreen));
    });
    window.setTimeout(() => { if (state.cy) { state.cy.resize(); state.cy.fit(undefined, 38); } }, 100);
  }

  function download(url, filename) {
    const link = document.createElement("a"); link.href = url; link.download = filename; document.body.appendChild(link); link.click(); link.remove();
  }

  async function search() {
    const input = q("[data-search]"); const results = q("[data-search-results]");
    if (!input || !results) return;
    const query = input.value.trim();
    if (!query) { results.hidden = true; return; }
    results.hidden = false; results.innerHTML = '<p class="muted">Searching…</p>';
    try {
      const params = graphParams(); params.set("q", query); params.set("limit", "12");
      const payload = await requestJson(`${api.search}?${params}`);
      results.innerHTML = payload.items.length ? payload.items.map((node) => `<button type="button" data-search-node="${esc(node.id)}"><strong>${esc(node.label)}</strong><small>${esc(node.metadata.department || pretty(node.metadata.document_kind))}${node.metadata.year ? ` · ${node.metadata.year}` : ""}</small></button>`).join("") : '<p class="muted">No matching documents in this view.</p>';
    } catch (error) { results.innerHTML = `<p class="muted">${esc(error.message)}</p>`; }
  }

  function focusSearchResult(nodeId) {
    const result = state.cy && state.cy.getElementById(nodeId);
    const results = q("[data-search-results]");
    if (results) results.hidden = true;
    if (result && result.length) {
      state.cy.animate({center: {eles: result}, zoom: 1.6}, {duration: 320}); selectElement(result, "node");
    } else loadNeighborhood(Number(nodeId.split(":")[1]));
  }

  function setAutoRefresh(seconds) {
    if (state.timer) window.clearInterval(state.timer);
    state.refreshSeconds = Number(seconds); state.timer = null;
    const refreshStatus = q("[data-refresh-status]");
    if (refreshStatus) refreshStatus.textContent = state.refreshSeconds ? `Auto refresh every ${state.refreshSeconds}s` : "Auto refresh off";
    if (state.refreshSeconds) state.timer = window.setInterval(() => refreshCurrent(), state.refreshSeconds * 1000);
  }

  function on(selector, eventName, handler, scope = root) {
    qa(selector, scope).forEach((element) => element.addEventListener(eventName, handler));
  }

  function bindEvents() {
    on("[data-refresh], [data-workspace-refresh]", "click", () => refreshCurrent());
    on("[data-auto-refresh]", "change", (event) => setAutoRefresh(event.target.value));
    on("[data-apply]", "click", () => {
      const component = q("[data-component]");
      state.componentId = component ? component.value : "";
      updateActiveFilterCount(); loadGraph({forceLayout: true});
    });
    on("[data-reset]", "click", resetFilters);
    on("[data-scope]", "click", (event) => setScope(event.currentTarget.dataset.scope));
    on("[data-connectivity]", "click", (event) => setConnectivity(event.currentTarget.dataset.connectivity));
    on("[data-component]", "change", (event) => { state.componentId = event.target.value; updateActiveFilterCount(); loadGraph({forceLayout: true}); });
    on("[data-layout]", "change", (event) => { state.layout = event.target.value; relayout(true); });
    on("[data-depth]", "change", (event) => { state.depth = Number(event.target.value); });
    on("[data-direction]", "change", (event) => { state.direction = event.target.value; });
    on("[data-full-graph]", "click", () => loadGraph({forceLayout: true}));
    on("[data-neighborhood]", "click", () => {
      if (state.selectedType === "node" && state.selectedId) loadNeighborhood(Number(state.selectedId.split(":")[1]));
    });
    on("[data-edge-labels]", "change", (event) => { state.edgeLabels = event.target.checked; updateEdgeLabels(); });
    on("[data-fit]", "click", () => state.cy && state.cy.fit(undefined, 38));
    on("[data-relayout]", "click", () => relayout(true));
    on("[data-clear-selection]", "click", clearSelection);
    on("[data-filter-toggle]", "click", () => togglePanel("filters"));
    on("[data-inspector-toggle]", "click", () => togglePanel("inspector"));
    window.addEventListener("app:sidebarchange", (event) => {
      state.sidebarCollapsed = Boolean(event.detail && event.detail.collapsed);
      window.setTimeout(() => { if (state.cy) { state.cy.resize(); state.cy.fit(undefined, 34); } }, 190);
    });
    on("[data-focus-mode], [data-workspace-focus]", "click", () => toggleFocus());
    on("[data-fullscreen], [data-workspace-fullscreen]", "click", toggleFullscreen);
    on("[data-export]", "click", () => download(`${api.graph}?${graphParams()}&download=1`, "knowledge-graph-filtered.json"));
    on("[data-export-png]", "click", () => { if (state.cy) download(state.cy.png({full: true, scale: 2, bg: "#ffffff"}), "knowledge-graph.png"); });
    on("[data-search-button]", "click", search);
    on("[data-search]", "keydown", (event) => { if (event.key === "Enter") search(); });
    on("[data-search-results]", "click", (event) => { const button = event.target.closest("[data-search-node]"); if (button) focusSearchResult(button.dataset.searchNode); });
    on("[data-summary-panel]", "toggle", (event) => { state.summaryCollapsed = !event.currentTarget.open; });
    on("[data-insights-panel]", "toggle", (event) => { state.insightsCollapsed = !event.currentTarget.open; });
    on("[data-legend]", "toggle", (event) => { state.legendOpen = event.currentTarget.open; });
    on("[data-advanced-filters]", "toggle", (event) => { state.advancedFiltersOpen = event.currentTarget.open; });
    root.addEventListener("click", (event) => { const rank = event.target.closest("[data-rank-document]"); if (rank) loadNeighborhood(Number(rank.dataset.rankDocument)); });
    document.addEventListener("fullscreenchange", () => {
      state.fullscreen = Boolean(document.fullscreenElement);
      qa("[data-fullscreen], [data-workspace-fullscreen]").forEach((button) => {
        button.textContent = state.fullscreen ? "Exit fullscreen" : "Fullscreen";
        button.setAttribute("aria-pressed", String(state.fullscreen));
      });
      window.setTimeout(() => { if (state.cy) { state.cy.resize(); state.cy.fit(undefined, 38); } }, 80);
    });
    window.addEventListener("resize", () => state.cy && state.cy.resize());
    document.addEventListener("keydown", (event) => {
      const target = event.target;
      if (event.key === "Escape") {
        if (state.focusMode) toggleFocus(false);
        if (state.fullscreenFallback) toggleFullscreen();
        const results = q("[data-search-results]");
        if (results) results.hidden = true;
      }
      if (event.key.toLowerCase() === "f" && !/input|select|textarea/i.test(target.tagName)) { event.preventDefault(); toggleFocus(); }
      if (event.key === "/" && !/input|select|textarea/i.test(target.tagName)) {
        const searchInput = q("[data-search]");
        if (searchInput) { event.preventDefault(); searchInput.focus(); }
      }
    });
  }

  async function initialize() {
    diagnostic("BOOTSTRAP");
    const missing = validateDomContract();
    if (missing.length) {
      diagnostic("CLIENT_RENDER_ERROR", {message: `Missing required DOM elements: ${missing.join(", ")}`});
      announce("The graph workspace is incomplete. Reload the page to restore it.", true, "Retry", "CLIENT_RENDER_ERROR");
      return;
    }
    if (typeof cytoscape !== "function") {
      diagnostic("CLIENT_RENDER_ERROR", {message: "Cytoscape is unavailable"});
      announce("The local graph renderer could not be loaded.", true, "Retry", "CLIENT_RENDER_ERROR");
      return;
    }
    bindEvents();
    updateActiveFilterCount();
    syncNeighborhoodAction();
    const componentsPromise = requestJson(api.components)
      .then(updateComponents)
      .catch((error) => {
        diagnostic("OPTIONAL_COMPONENTS_ERROR", {message: error.message});
        const component = q("[data-component]");
        if (component) component.disabled = true;
      });
    await loadGraph({forceLayout: true});
    await componentsPromise;
  }

  window.__kgExplorerReady = initialize();
})();
