# Graph-aware retrieval V1

Graph retrieval is an experimental read-only layer after the unchanged dense
BAAI/bge-m3 retriever. Production remains `RETRIEVAL_MODE=dense`.

```text
query
  -> unchanged 1,024-dimension dense retrieval
  -> unique dense seed documents (Top-1, Top-3, or Top-5)
  -> one-hop validated REFERENCES lookup
  -> unique related documents plus relation provenance
  -> optional dense page lookup restricted to each graph-selected document
```

The PostgreSQL vector query, scores, chunk Top-K semantics, filters, and frozen
29-query benchmark are unchanged. Graph traversal is not placed in vector SQL,
does not create or modify edges, and never assigns graph candidates a cosine or
synthetic fusion score.

## Expansion contract

V1 admits only document-to-document `REFERENCES` relations in
`AUTO_VERIFIED` or genuine `HUMAN_CONFIRMED` state. `ISSUED_BY`, unresolved
candidates, strong-relation signals, semantic similarity, and every other
relation type are excluded. Expansion is exactly one hop. Outgoing means a
dense seed points to a document it references; incoming means a document that
references the seed; bidirectional is their transparent union.

Every graph path retains seed document, dense rank and similarity, relation ID,
type, direction and validation state, source evidence page and excerpt,
candidate ID, source and target document IDs, and distance 1. A neighbor already
in dense results is `ALREADY_IN_DENSE` corroboration rather than a duplicate.
New candidates are deterministically ordered by seed dense rank, relation type,
neighbor document ID, relation ID, and direction. Per-seed and total limits are
explicit, and limit hits are reported.

The relation evidence page proves the edge. It is not assumed to answer the
query. The optional restricted dense lookup identifies a candidate content page
inside the graph-selected document and is reported separately.

## Evaluation and decision

The evidence-derived graph benchmark has 40 cases labelled
`AUTO_VERIFIED_SOURCE_EVIDENCE`; it is not independent human ground truth.
The frozen dense benchmark remains a separate semantic-retrieval test. Graph
Recovery Rate is recovered eligible dense misses divided by eligible cases.
Graph Expansion Precision is the single evidence-labelled useful addition
divided by every new graph addition evaluated for those cases.

The best measured variant was bidirectional Top-1: 9/9 eligible cases recovered,
9/63 additions useful under the single-label benchmark (14.29%), 100% provenance,
1.575 average additions/query, and about 1.39 seconds P95 total local latency.
Dense results did not regress or move. Because recovery is real but precision is
not yet safe for answer generation, the gate is
`KEEP_DENSE_AND_HARDEN_GRAPH_RETRIEVAL`; RAG and LLM integration remain
unimplemented.

Evidence is under `outputs/graph_retrieval_v1/` and the frozen graph benchmark
is `data/retrieval_eval_graph_v1.csv`.
