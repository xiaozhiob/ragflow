#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from api.db import LLMType
from api.db.services.llm_service import LLMBundle
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.settings import retrievaler
from api.utils import get_uuid
from api.utils.file_utils import get_project_base_directory
from rag.nlp import tokenize, search
from rag.utils.es_conn import ELASTICSEARCH
from ranx import evaluate
import pandas as pd
from tqdm import tqdm


class Benchmark:
    def __init__(self, kb_id):
        e, self.kb = KnowledgebaseService.get_by_id(kb_id)
        self.similarity_threshold = self.kb.similarity_threshold
        self.vector_similarity_weight = self.kb.vector_similarity_weight
        self.embd_mdl = LLMBundle(self.kb.tenant_id, LLMType.EMBEDDING, llm_name=self.kb.embd_id, lang=self.kb.language)

    def _get_benchmarks(self, query, dataset_idxnm, count=16):

        req = {"question": query, "size": count, "vector": True, "similarity": self.similarity_threshold}
        sres = retrievaler.search(req, search.index_name(dataset_idxnm), self.embd_mdl)
        return sres

    def _get_retrieval(self, qrels, dataset_idxnm):
        run = defaultdict(dict)
        query_list = list(qrels.keys())
        for query in query_list:

            ranks = retrievaler.retrieval(query, self.embd_mdl, dataset_idxnm.replace("ragflow_", ""),
                                          [self.kb.id], 0, 30,
                           0.0, self.vector_similarity_weight)
            for c in ranks["chunks"]:
                if "vector" in c:
                    del c["vector"]
                run[query][c["chunk_id"]] = c["similarity"]

        return run

    def embedding(self, docs, batch_size=16):
        vects = []
        cnts = [d["content_with_weight"] for d in docs]
        for i in range(0, len(cnts), batch_size):
            vts, c = self.embd_mdl.encode(cnts[i: i + batch_size])
            vects.extend(vts.tolist())
        assert len(docs) == len(vects)
        for i, d in enumerate(docs):
            v = vects[i]
            d["q_%d_vec" % len(v)] = v
        return docs

    @staticmethod
    def init_kb(index_name):
        idxnm = search.index_name(index_name)
        if ELASTICSEARCH.indexExist(idxnm):
            ELASTICSEARCH.deleteIdx(search.index_name(index_name))

        return ELASTICSEARCH.createIdx(idxnm, json.load(
            open(os.path.join(get_project_base_directory(), "conf", "mapping.json"), "r")))

    def ms_marco_index(self, file_path, index_name):
        qrels = defaultdict(dict)
        texts = defaultdict(dict)
        docs = []
        filelist = os.listdir(file_path)
        self.init_kb(index_name)

        max_workers = int(os.environ.get('MAX_WORKERS', 3))
        exe = ThreadPoolExecutor(max_workers=max_workers)
        threads = []

        def slow_actions(es_docs, idx_nm):
            es_docs = self.embedding(es_docs)
            ELASTICSEARCH.bulk(es_docs, idx_nm)
            return True

        for dir in filelist:
            data = pd.read_parquet(os.path.join(file_path, dir))
            for i in tqdm(range(len(data)), colour="green", desc="Tokenizing:" + dir):

                query = data.iloc[i]['query']
                for rel, text in zip(data.iloc[i]['passages']['is_selected'], data.iloc[i]['passages']['passage_text']):
                    d = {
                        "id": get_uuid(),
                        "kb_id": self.kb.id
                    }
                    tokenize(d, text, "english")
                    docs.append(d)
                    texts[d["id"]] = text
                    qrels[query][d["id"]] = int(rel)
                if len(docs) >= 32:
                    threads.append(
                        exe.submit(slow_actions, deepcopy(docs), search.index_name(index_name)))
                    docs = []

        threads.append(
            exe.submit(slow_actions, deepcopy(docs), search.index_name(index_name)))

        for i in tqdm(range(len(threads)), colour="red", desc="Indexing:" + dir):
            if not threads[i].result().output:
                print("Indexing error...")

        return qrels, texts

    def trivia_qa_index(self, file_path, index_name):
        qrels = defaultdict(dict)
        texts = defaultdict(dict)
        docs = []
        filelist = os.listdir(file_path)
        for dir in filelist:
            data = pd.read_parquet(os.path.join(file_path, dir))
            for i in tqdm(range(len(data)), colour="green", desc="Indexing:" + dir):
                query = data.iloc[i]['question']
                for rel, text in zip(data.iloc[i]["search_results"]['rank'],
                                     data.iloc[i]["search_results"]['search_context']):
                    d = {
                        "id": get_uuid()
                    }
                    tokenize(d, text, "english")
                    docs.append(d)
                    texts[d["id"]] = text
                    qrels[query][d["id"]] = int(rel)
                if len(docs) >= 32:
                    docs = self.embedding(docs)
                    ELASTICSEARCH.bulk(docs, search.index_name(index_name))
                    docs = []

        docs = self.embedding(docs)
        ELASTICSEARCH.bulk(docs, search.index_name(index_name))
        return qrels, texts

    def miracl_index(self, file_path, corpus_path, index_name):

        corpus_total = {}
        for corpus_file in os.listdir(corpus_path):
            tmp_data = pd.read_json(os.path.join(corpus_path, corpus_file), lines=True)
            for index, i in tmp_data.iterrows():
                corpus_total[i['docid']] = i['text']

        topics_total = {}
        for topics_file in os.listdir(os.path.join(file_path, 'topics')):
            if 'test' in topics_file:
                continue
            tmp_data = pd.read_csv(os.path.join(file_path, 'topics', topics_file), sep='\t', names=['qid', 'query'])
            for index, i in tmp_data.iterrows():
                topics_total[i['qid']] = i['query']

        qrels = defaultdict(dict)
        texts = defaultdict(dict)
        docs = []
        for qrels_file in os.listdir(os.path.join(file_path, 'qrels')):
            if 'test' in qrels_file:
                continue

            tmp_data = pd.read_csv(os.path.join(file_path, 'qrels', qrels_file), sep='\t',
                                   names=['qid', 'Q0', 'docid', 'relevance'])
            for i in tqdm(range(len(tmp_data)), colour="green", desc="Indexing:" + qrels_file):
                query = topics_total[tmp_data.iloc[i]['qid']]
                text = corpus_total[tmp_data.iloc[i]['docid']]
                rel = tmp_data.iloc[i]['relevance']
                d = {
                    "id": get_uuid()
                }
                tokenize(d, text, 'english')
                docs.append(d)
                texts[d["id"]] = text
                qrels[query][d["id"]] = int(rel)
                if len(docs) >= 32:
                    docs = self.embedding(docs)
                    ELASTICSEARCH.bulk(docs, search.index_name(index_name))
                    docs = []

        docs = self.embedding(docs)
        ELASTICSEARCH.bulk(docs, search.index_name(index_name))

        return qrels, texts

    def save_results(self, qrels, run, texts, dataset, file_path):
        keep_result = []
        run_keys = list(run.keys())
        for run_i in tqdm(range(len(run_keys)), desc="Calculating ndcg@10 for single query"):
            key = run_keys[run_i]
            keep_result.append({'query': key, 'qrel': qrels[key], 'run': run[key],
                                'ndcg@10': evaluate({key: qrels[key]}, {key: run[key]}, "ndcg@10")})
        keep_result = sorted(keep_result, key=lambda kk: kk['ndcg@10'])
        with open(os.path.join(file_path, dataset + 'result.md'), 'w', encoding='utf-8') as f:
            f.write('## Score For Every Query\n')
            for keep_result_i in keep_result:
                f.write('### query: ' + keep_result_i['query'] + ' ndcg@10:' + str(keep_result_i['ndcg@10']) + '\n')
                scores = [[i[0], i[1]] for i in keep_result_i['run'].items()]
                scores = sorted(scores, key=lambda kk: kk[1])
                for score in scores[:10]:
                    f.write('- text: ' + str(texts[score[0]]) + '\t qrel: ' + str(score[1]) + '\n')
        json.dump(qrels, open(os.path.join(file_path, dataset + '.qrels.json'), "w+"), indent=2)
        json.dump(run, open(os.path.join(file_path, dataset + '.run.json'), "w+"), indent=2)
        print(os.path.join(file_path, dataset + '_result.md'), 'Saved!')

    def __call__(self, dataset, file_path, miracl_corpus=''):
        if dataset == "ms_marco_v1.1":
            qrels, texts = self.ms_marco_index(file_path, "benchmark_ms_marco_v1.1")
            run = self._get_retrieval(qrels, "benchmark_ms_marco_v1.1")
            print(dataset, evaluate(qrels, run, ["ndcg@10", "map@5", "mrr"]))
            self.save_results(qrels, run, texts, dataset, file_path)
        if dataset == "trivia_qa":
            qrels, texts = self.trivia_qa_index(file_path, "benchmark_trivia_qa")
            run = self._get_retrieval(qrels, "benchmark_trivia_qa")
            print(dataset, evaluate(qrels, run, ["ndcg@10", "map@5", "mrr"]))
            self.save_results(qrels, run, texts, dataset, file_path)
        if dataset == "miracl":
            for lang in ['ar', 'bn', 'de', 'en', 'es', 'fa', 'fi', 'fr', 'hi', 'id', 'ja', 'ko', 'ru', 'sw', 'te', 'th',
                         'yo', 'zh']:
                if not os.path.isdir(os.path.join(file_path, 'miracl-v1.0-' + lang)):
                    print('Directory: ' + os.path.join(file_path, 'miracl-v1.0-' + lang) + ' not found!')
                    continue
                if not os.path.isdir(os.path.join(file_path, 'miracl-v1.0-' + lang, 'qrels')):
                    print('Directory: ' + os.path.join(file_path, 'miracl-v1.0-' + lang, 'qrels') + 'not found!')
                    continue
                if not os.path.isdir(os.path.join(file_path, 'miracl-v1.0-' + lang, 'topics')):
                    print('Directory: ' + os.path.join(file_path, 'miracl-v1.0-' + lang, 'topics') + 'not found!')
                    continue
                if not os.path.isdir(os.path.join(miracl_corpus, 'miracl-corpus-v1.0-' + lang)):
                    print('Directory: ' + os.path.join(miracl_corpus, 'miracl-corpus-v1.0-' + lang) + ' not found!')
                    continue
                qrels, texts = self.miracl_index(os.path.join(file_path, 'miracl-v1.0-' + lang),
                                                 os.path.join(miracl_corpus, 'miracl-corpus-v1.0-' + lang),
                                                 "benchmark_miracl_" + lang)
                run = self._get_retrieval(qrels, "benchmark_miracl_" + lang)
                print(dataset, evaluate(qrels, run, ["ndcg@10", "map@5", "mrr"]))
                self.save_results(qrels, run, texts, dataset, file_path)


if __name__ == '__main__':
    print('*****************RAGFlow Benchmark*****************')
    kb_id = input('Please input kb_id:\n')
    ex = Benchmark(kb_id)
    dataset = input(
        'RAGFlow Benchmark Support:\n\tms_marco_v1.1:<https://huggingface.co/datasets/microsoft/ms_marco>\n\ttrivia_qa:<https://huggingface.co/datasets/mandarjoshi/trivia_qa>\n\tmiracl:<https://huggingface.co/datasets/miracl/miracl>\nPlease input dataset choice:\n')
    if dataset in ['ms_marco_v1.1', 'trivia_qa']:
        if dataset == "ms_marco_v1.1":
            print("Notice: Please provide the ms_marco_v1.1 dataset only. ms_marco_v2.1 is not supported!")
        dataset_path = input('Please input ' + dataset + ' dataset path:\n')
        ex(dataset, dataset_path)
    elif dataset == 'miracl':
        dataset_path = input('Please input ' + dataset + ' dataset path:\n')
        corpus_path = input('Please input ' + dataset + '-corpus dataset path:\n')
        ex(dataset, dataset_path, miracl_corpus=corpus_path)
    else:
        print("Dataset: ", dataset, "not supported!")
