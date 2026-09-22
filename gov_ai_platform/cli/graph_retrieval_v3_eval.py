"""Evidence feasibility first; no develop/freeze/test command before manual review."""
import argparse
import json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage',choices=('initialize','measure','finalize-saved'))
    args = parser.parse_args()
    from knowledge_engine.retrieval.graph_v3_io import confine_runtime_writes
    confine_runtime_writes()
    from knowledge_engine.retrieval.graph_v3_audit import initialize, measure, finalize_saved
    result = {'initialize':initialize,'measure':measure,'finalize-saved':finalize_saved}[args.stage]()
    print(json.dumps(result,ensure_ascii=True,indent=2))


if __name__ == '__main__':
    main()
