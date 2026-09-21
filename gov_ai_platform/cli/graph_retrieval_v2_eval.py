from __future__ import annotations

import argparse
import json
import os
import sys
from time import perf_counter


def main():
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    parser = argparse.ArgumentParser(description='Graph V2 staged read-only engineering evaluation')
    parser.add_argument('command',choices=('initialize','baseline','develop','validate','freeze','test','finalize','report'))
    args = parser.parse_args()
    os.environ['PGOPTIONS']='-c default_transaction_read_only=on'
    os.environ['HF_HUB_OFFLINE']='1'
    os.environ['TRANSFORMERS_OFFLINE']='1'
    from knowledge_engine.retrieval.graph_v2_protocol import initialize, native_guard
    native_guard()
    from knowledge_engine.retrieval.graph_v2_eval import develop, freeze, locked_test, finalize_measurements
    if args.command=='initialize':
        result=initialize()
    elif args.command=='freeze':
        result=freeze()
    elif args.command=='validate':
        from knowledge_engine.retrieval.graph_v2_report import validate
        result=validate()
    elif args.command=='report':
        from knowledge_engine.retrieval.graph_v2_report import report
        result=report()
    else:
        from knowledge_engine.embeddings.bge_m3 import get_embedder
        from knowledge_engine.retrieval.graph_v2_measure import baseline
        started=perf_counter()
        embedder=get_embedder()
        print(f'Warm-model measurement; model initialization excluded: {perf_counter()-started:.3f} s',flush=True)
        result={'baseline':baseline,'develop':develop,'test':locked_test,'finalize':finalize_measurements}[args.command](embedder)
    print(json.dumps(result,ensure_ascii=False,indent=2,default=str))


if __name__=='__main__':
    main()
