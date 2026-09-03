(() => {
  'use strict';
  const root = document.querySelector('[data-graph-explorer]');
  if (!root) return;
  const api = JSON.parse(document.getElementById('kg-explorer-api').textContent);
  const initialSummary = JSON.parse(document.getElementById('kg-explorer-initial-summary').textContent);
  const $ = selector => root.querySelector(selector);
  const $$ = selector => [...root.querySelectorAll(selector)];
  const state = { cy: null, summary: initialSummary, mode: 'full', rootDocumentId: null, timer: null, selectedNode: null };
  const message = $('[data-graph-message]');
  const detail = document.querySelector('[data-graph-detail]');

  const text = (parent, tag, value, className = '') => { const element = document.createElement(tag); if (className) element.className = className; element.textContent = value == null || value === '' ? '—' : String(value); parent.appendChild(element); return element; };
  const fetchJson = async url => { const response = await fetch(url, { headers: { Accept: 'application/json' }, cache: 'no-store' }); const payload = await response.json(); if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`); return payload; };
  const setBusy = (busy, label = 'Loading current graph…') => { message.hidden = !busy; message.textContent = label; $('[data-refresh]').disabled = busy; };
  const shortDate = value => value ? new Date(value).toLocaleString() : '—';
  const filters = () => { const params = new URLSearchParams(); $$('input[name="relation"]:checked').forEach(input => params.append('relation', input.value)); $$('[data-filter]').forEach(input => { if (input.value) params.set(input.dataset.filter, input.value); }); if ($('[data-component]').value) params.set('component', $('[data-component]').value); return params; };
  const graphUrl = () => { const params = filters(); if (state.mode === 'neighborhood' && state.rootDocumentId) { params.set('depth', $('[data-depth]').value); params.set('direction', $('[data-direction]').value); return api.neighborhood_template.replace('/0/', `/${state.rootDocumentId}/`) + `?${params}`; } return api.graph + `?${params}`; };

  const updateSummary = summary => {
    state.summary = summary;
    $$('[data-metric]').forEach(element => { const value = summary[element.dataset.metric]; if (value != null) element.textContent = value; });
    $$('[data-relation-count]').forEach(element => { element.textContent = summary.relation_counts[element.dataset.relationCount] || 0; });
    $$('[data-filter-relation-count]').forEach(element => { element.textContent = summary.relation_counts[element.dataset.filterRelationCount] || 0; });
    $$('[data-coverage]').forEach(element => { element.textContent = `${(summary.graph_coverage * 100).toFixed(1)}%`; });
    $$('[data-coverage-long]').forEach(element => { element.textContent = `${summary.connected_documents}/${summary.documents} (${(summary.graph_coverage * 100).toFixed(1)}%)`; });
    $('[data-generated-at]').textContent = shortDate(summary.generated_at); $('[data-fingerprint]').textContent = summary.graph_fingerprint.slice(0, 12);
    fillRanked('[data-top-cited]', summary.top_cited, 'incoming'); fillRanked('[data-top-citing]', summary.top_citing, 'outgoing'); fillRanked('[data-most-connected]', summary.most_connected, 'degree'); fillRanked('[data-department-counts]', summary.department_document_counts.slice(0, 5), 'department'); populateOptions(summary.filter_options);
  };
  const fillRanked = (selector, items, kind) => { const list = $(selector); list.replaceChildren(); items.forEach(item => { const li = document.createElement('li'); const label = kind === 'department' ? item.department : item.label; text(li, 'strong', label); li.append(` · ${item.count}`); list.appendChild(li); }); };
  const addOptions = (select, values) => { const current = select.value; [...select.options].slice(1).forEach(option => option.remove()); values.forEach(value => { const option = document.createElement('option'); option.value = value; option.textContent = value; select.appendChild(option); }); if ([...select.options].some(option => option.value === current)) select.value = current; };
  const populateOptions = options => { addOptions($('[data-filter="kind"]'), options.document_kinds); addOptions($('[data-filter="department"]'), options.departments); addOptions($('[data-filter="validation"]'), options.validation_states); addOptions($('[data-filter="source_origin"]'), options.source_origins); };

  const cyStyle = [
    { selector: 'node', style: { label: 'data(label)', 'font-size': 9, 'text-wrap': 'ellipsis', 'text-max-width': 105, 'text-valign': 'bottom', 'text-margin-y': 7, color: '#26352f', 'background-color': '#dcecf7', 'border-color': '#315f8a', 'border-width': 2, width: 34, height: 34 } },
    { selector: 'node[type="DOCUMENT"]', style: { shape: 'round-rectangle' } }, { selector: 'node[type="DEPARTMENT"]', style: { shape: 'diamond', 'background-color': '#fff0d2', 'border-color': '#9b6518', width: 42, height: 42 } },
    { selector: 'node.isolated', style: { 'border-style': 'dotted', 'background-color': '#fff' } }, { selector: 'node:selected', style: { 'border-color': '#d08a25', 'border-width': 4, 'overlay-opacity': .08 } },
    { selector: 'edge', style: { width: 1.7, 'curve-style': 'bezier', 'target-arrow-shape': 'triangle', 'arrow-scale': .8, label: 'data(relation_type)', 'font-size': 7, 'text-background-color': '#fff', 'text-background-opacity': .8, 'text-background-padding': 2, 'line-color': '#176b4a', 'target-arrow-color': '#176b4a' } },
    { selector: 'edge[relation_type="ISSUED_BY"]', style: { 'line-style': 'dashed', 'line-color': '#9b6518', 'target-arrow-color': '#9b6518', 'target-arrow-shape': 'vee' } },
    { selector: 'edge[relation_type="AMENDS"],edge[relation_type="CORRECTS"]', style: { width: 2.5, 'line-style': 'dashed', 'line-color': '#6b4c86', 'target-arrow-color': '#6b4c86' } },
    { selector: 'edge[relation_type="SUPERSEDES"],edge[relation_type="PARTIALLY_SUPERSEDES"],edge[relation_type="CANCELS"]', style: { width: 2.5, 'line-style': 'dotted', 'line-color': '#a73b32', 'target-arrow-color': '#a73b32', 'target-arrow-shape': 'tee' } },
    { selector: 'edge[relation_type="EXTENDS"]', style: { width: 2.5, 'line-style': 'dashed', 'line-color': '#315f8a', 'target-arrow-color': '#315f8a' } },
    { selector: 'edge:selected', style: { width: 4, 'line-color': '#d08a25', 'target-arrow-color': '#d08a25' } }, { selector: '.search-hit', style: { 'border-color': '#d08a25', 'border-width': 5, 'background-color': '#fff4d9' } },
  ];
  const renderGraph = payload => {
    const elements = [...payload.nodes.map(node => ({ group: 'nodes', data: { ...node, metadata: node.metadata }, classes: node.type === 'DOCUMENT' && node.degree.degree === 0 ? 'isolated' : '' })), ...payload.edges.map(edge => ({ group: 'edges', data: edge }))];
    if (state.cy) state.cy.destroy(); state.cy = cytoscape({ container: document.getElementById('knowledge-graph-canvas'), elements, style: cyStyle, minZoom: .15, maxZoom: 3, wheelSensitivity: .2 }); layout();
    state.cy.on('tap', 'node', event => selectNode(event.target)); state.cy.on('tap', 'edge', event => selectEdge(event.target)); state.cy.on('mouseover', 'node,edge', event => showTooltip(event)); state.cy.on('mouseout', 'node,edge', hideTooltip);
    $('[data-visible-nodes]').textContent = payload.returned.nodes; $('[data-visible-edges]').textContent = payload.returned.edges; $('[data-mode-label]').textContent = payload.mode === 'full' ? 'Full graph' : `${payload.mode_metadata.depth}-hop ${payload.mode_metadata.direction}`;
    $('[data-truncation]').hidden = !payload.truncated; $('[data-truncation]').textContent = payload.truncated ? `Response truncated: ${payload.truncation_reason}` : ''; updateSummary(payload.summary); setBusy(false);
  };
  const layout = () => state.cy?.layout({ name: 'cose', animate: false, randomize: true, componentSpacing: 80, nodeRepulsion: () => 7500, idealEdgeLength: () => 95, gravity: .25, numIter: 650 }).run();
  const loadGraph = async () => { setBusy(true, 'Refreshing current PostgreSQL graph…'); try { renderGraph(await fetchJson(graphUrl())); } catch (error) { setBusy(true, `Graph unavailable: ${error.message}`); } };
  const refreshAll = async () => { try { const [summary, components] = await Promise.all([fetchJson(api.summary), fetchJson(api.components)]); updateSummary(summary); populateComponents(components.items); await loadGraph(); } catch (error) { setBusy(true, `Refresh failed: ${error.message}`); } };
  const populateComponents = items => { const select = $('[data-component]'), current = select.value; [...select.options].slice(1).forEach(option => option.remove()); items.forEach((item, index) => { const option = document.createElement('option'); option.value = item.component_id; option.textContent = `${index + 1}. ${item.document_count} docs · ${item.edge_count} edges${item.largest ? ' · largest' : ''}`; select.appendChild(option); }); if ([...select.options].some(option => option.value === current)) select.value = current; };
  const showTooltip = event => { const tooltip = $('[data-tooltip]'), data = event.target.data(); tooltip.replaceChildren(); if (event.target.isNode()) { text(tooltip, 'strong', data.label); text(tooltip, 'div', `${data.type} · degree ${data.degree.degree}`); if (data.metadata?.department) text(tooltip, 'div', data.metadata.department); } else { text(tooltip, 'strong', `${data.relation_type} →`); text(tooltip, 'div', `${data.validation_state} · page ${data.provenance_summary.source_page || '—'}`); } const position = event.renderedPosition || { x: 20, y: 20 }; tooltip.style.left = `${Math.min(position.x + 15, $('.graph-stage-card').clientWidth - 310)}px`; tooltip.style.top = `${position.y + 70}px`; tooltip.hidden = false; };
  const hideTooltip = () => { $('[data-tooltip]').hidden = true; };
  const detailGrid = entries => { const dl = document.createElement('dl'); dl.className = 'detail-grid'; entries.forEach(([label, value]) => { const row = document.createElement('div'); text(row, 'dt', label); text(row, 'dd', value); dl.appendChild(row); }); return dl; };
  const selectNode = async node => { state.selectedNode = node.id(); const metadata = node.data('metadata'); state.rootDocumentId = node.data('type') === 'DOCUMENT' ? metadata.document_id : null; $('[data-neighborhood]').disabled = !state.rootDocumentId; detail.replaceChildren(); text(detail, 'span', node.data('type'), 'detail-kicker'); text(detail, 'h3', node.data('label'), 'detail-title'); text(detail, 'p', 'Loading current node details…', 'muted'); document.body.classList.remove('context-collapsed'); try { renderNodeDetails(await fetchJson(api.node_template.replace('node-placeholder', node.id()))); } catch (error) { text(detail, 'p', error.message, 'alert error'); } };
  const renderNodeDetails = payload => {
    detail.replaceChildren(); text(detail, 'span', payload.type, 'detail-kicker');
    if (payload.type === 'DOCUMENT') {
      const d = payload.document, incomingReferences = payload.incoming_relations.filter(item => item.relation_type === 'REFERENCES'), outgoingReferences = payload.outgoing_relations.filter(item => item.relation_type === 'REFERENCES');
      text(detail, 'h3', d.gr_number || d.canonical_identity || d.file_name, 'detail-title');
      if (d.subject) text(detail, 'p', d.subject, 'muted');
      detail.appendChild(detailGrid([
        ['Document ID', d.id], ['Kind', d.document_kind || d.document_type], ['Date / year', d.gr_date || d.document_year],
        ['Department', d.department], ['Source origin', d.source_origin], ['Pages / chunks', `${d.page_count} / ${d.chunk_count}`],
        ['Incoming REFERENCES', incomingReferences.length], ['Outgoing REFERENCES', outgoingReferences.length],
        ['Other relation counts', Object.entries(payload.relation_counts).filter(([key]) => key !== 'REFERENCES').map(([key, value]) => `${key}: ${value}`).join(', ')],
        ['Connected departments', payload.connected_departments.join(', ')], ['SHA-256', d.sha256], ['Stored path', d.file_path],
        ['Archive path', d.archive_relative_path], ['Source authority', d.source_authority], ['Source domain', d.source_domain], ['Official source URL', d.final_url || d.source_url],
      ]));
      const links = document.createElement('div'); links.className = 'detail-links';
      const doc = text(links, 'a', 'Open document workspace'); doc.href = `/documents/${d.id}/`;
      const pdf = text(links, 'a', 'Open PDF'); pdf.href = `/documents/${d.id}/pdf/`; pdf.target = '_blank'; pdf.rel = 'noopener'; detail.appendChild(links);
      if (payload.neighboring_documents.length) {
        text(detail, 'h4', 'Neighboring documents'); const neighbors = document.createElement('div'); neighbors.className = 'detail-neighbors';
        payload.neighboring_documents.forEach(node => { const button = text(neighbors, 'button', node.label); button.type = 'button'; button.addEventListener('click', () => focusSearchNode(node)); }); detail.appendChild(neighbors);
      }
      const evidenceRelations = [...incomingReferences, ...outgoingReferences].filter(item => item.provenance_summary.source_page);
      if (evidenceRelations.length) {
        text(detail, 'h4', 'Evidence pages'); const evidenceLinks = document.createElement('div'); evidenceLinks.className = 'detail-links';
        evidenceRelations.forEach(item => { const sourceId = item.source.split(':')[1]; const link = text(evidenceLinks, 'a', `${item.relation_type} · page ${item.provenance_summary.source_page}`); link.href = `/documents/${sourceId}/pdf/#page=${item.provenance_summary.source_page}`; link.target = '_blank'; link.rel = 'noopener'; }); detail.appendChild(evidenceLinks);
      }
    } else {
      const d = payload.department; text(detail, 'h3', d.canonical_name, 'detail-title');
      detail.appendChild(detailGrid([['Department ID', d.id], ['Aliases', (d.aliases || []).join(', ')], ['Issued documents', d.document_count], ['Incoming ISSUED_BY', payload.incoming_relations.length], ['Outgoing relations', 0], ['Document IDs', payload.document_ids.join(', ')]]));
    }
  };
  const selectEdge = async edge => { state.selectedNode = null; state.rootDocumentId = null; $('[data-neighborhood]').disabled = true; detail.replaceChildren(); text(detail, 'span', 'Validated directed edge', 'detail-kicker'); text(detail, 'h3', `${edge.data('relation_type')} →`, 'detail-title'); text(detail, 'p', 'Loading stored provenance…', 'muted'); document.body.classList.remove('context-collapsed'); try { renderEdgeDetails(await fetchJson(api.edge_template.replace('/0/', `/${edge.data('relation_id')}/`))); } catch (error) { text(detail, 'p', error.message, 'alert error'); } };
  const renderEdgeDetails = payload => {
    detail.replaceChildren(); text(detail, 'span', 'Validated directed edge', 'detail-kicker'); text(detail, 'h3', `${payload.relation_type} →`, 'detail-title');
    detail.appendChild(detailGrid([
      ['Relation ID', payload.id], ['Direction', `${payload.source_document_id} → ${payload.target_document_id || `department:${payload.target_department_id}`}`],
      ['Source', payload.source_number || payload.source_file], ['Source origin', payload.source_origin], ['Source path', payload.source_path], ['Source SHA-256', payload.source_sha256],
      ['Target', payload.target_number || payload.target_file || payload.target_department], ['Target origin', payload.target_origin], ['Target path', payload.target_path], ['Target SHA-256', payload.target_sha256],
      ['Validation', payload.validation_status], ['Resolution', payload.evidence[0]?.resolution_status], ['Candidate ID', payload.created_from_candidate_id],
      ['Evidence links', payload.evidence.length], ['Created', shortDate(payload.created_at)], ['Provenance complete', payload.provenance_complete ? 'Yes' : 'No'],
    ]));
    payload.evidence.forEach(item => {
      const section = document.createElement('section'); text(section, 'strong', `Page ${item.source_page} · candidate ${item.candidate_id}`); text(section, 'div', item.raw_evidence_text, 'detail-evidence');
      section.appendChild(detailGrid([['Relation evidence ID', item.relation_evidence_id], ['Source document ID', item.source_document_id], ['Parser', item.parser_version], ['Pattern', item.pattern_id], ['Resolution state', item.resolution_status], ['Resolution method', item.resolution_method], ['Resolution evidence', item.resolution_evidence], ['Candidate validation', item.validation_status], ['Evidence hash', item.evidence_hash]]));
      const link = text(section, 'a', `Verify source page ${item.source_page} in PDF`, 'text-link'); link.href = `/documents/${item.source_document_id}/pdf/#page=${item.source_page}`; link.target = '_blank'; link.rel = 'noopener'; detail.appendChild(section);
    });
    if (!payload.evidence.length) text(detail, 'p', payload.relation_type === 'ISSUED_BY' ? 'Issuer provenance is the validated stored department relation and document metadata.' : 'No evidence link was returned.', 'muted');
  };
  const search = async () => { const query = $('[data-search]').value.trim(), results = $('[data-search-results]'); if (!query) { results.hidden = true; return; } try { const params = filters(); params.set('q', query); const payload = await fetchJson(`${api.search}?${params}`); results.replaceChildren(); if (!payload.items.length) text(results, 'p', 'No matching document nodes.', 'muted'); payload.items.forEach(node => { const button = text(results, 'button', `${node.label} · ${node.metadata.department}`); button.type = 'button'; button.addEventListener('click', () => focusSearchNode(node)); }); results.hidden = false; } catch (error) { results.replaceChildren(); text(results, 'p', error.message, 'alert error'); results.hidden = false; } };
  const focusSearchNode = node => { const existing = state.cy?.getElementById(node.id); if (existing?.length) { state.cy.elements().removeClass('search-hit'); existing.addClass('search-hit'); state.cy.animate({ center: { eles: existing }, zoom: 1.25, duration: 350 }); existing.select(); selectNode(existing); } else { state.rootDocumentId = node.metadata.document_id; state.mode = 'neighborhood'; loadGraph(); } $('[data-search-results]').hidden = true; };

  $('[data-refresh]').addEventListener('click', refreshAll); $('[data-apply]').addEventListener('click', () => { state.mode = 'full'; loadGraph(); });
  $('[data-reset]').addEventListener('click', () => { $$('input[name="relation"]').forEach(input => { input.checked = true; }); $$('[data-filter]').forEach(input => { input.value = input.dataset.filter === 'max_nodes' ? '500' : input.dataset.filter === 'max_edges' ? '2000' : ''; }); $('[data-filter="connectivity"]').value = 'all'; $('[data-filter="presence"]').value = 'any'; $('[data-component]').value = ''; state.mode = 'full'; loadGraph(); });
  $('[data-full-graph]').addEventListener('click', () => { state.mode = 'full'; state.rootDocumentId = null; $('[data-neighborhood]').disabled = true; loadGraph(); }); $('[data-neighborhood]').addEventListener('click', () => { if (state.rootDocumentId) { state.mode = 'neighborhood'; loadGraph(); } });
  $('[data-fit]').addEventListener('click', () => state.cy?.fit(undefined, 35)); $('[data-relayout]').addEventListener('click', layout); $('[data-search-button]').addEventListener('click', search); $('[data-search]').addEventListener('keydown', event => { if (event.key === 'Enter') search(); }); $('[data-component]').addEventListener('change', () => { state.mode = 'full'; loadGraph(); });
  $('[data-export]').addEventListener('click', () => { const params = filters(); params.set('download', '1'); window.location.href = `${api.graph}?${params}`; }); $('[data-auto-refresh]').addEventListener('change', event => { if (state.timer) clearInterval(state.timer); state.timer = null; const seconds = Number(event.target.value); if (seconds) state.timer = setInterval(refreshAll, seconds * 1000); });
  if (typeof cytoscape !== 'function') { setBusy(true, 'Local graph renderer failed to load.'); return; }
  updateSummary(initialSummary); refreshAll();
})();
