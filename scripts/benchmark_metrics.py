"""Reproducible local timing overhead check. No model calls or semantic review.

Copies four project source files to a new output directory and uses disposable
maps. Alternates identical read workloads with/without recording after warmup.
"""
import argparse
import json
from pathlib import Path
import platform
import shutil
from statistics import median
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from codemap.agent import AgentTools
from codemap.metrics import Recorder, report
from codemap.project import open_project


class Unrecorded(Recorder):
    def begin(self, *args, **kwargs):
        return self

    def finish(self, *args, **kwargs):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / 'source'; source.mkdir()
    for name in ('project.py', 'core.py', 'repository.py', 'store.py'):
        shutil.copyfile(ROOT / 'codemap' / name, source / name)
    tools = AgentTools(); tools.set_client_info({'name': '本地工具验收（未调用模型）'})
    project = tools.call('blueprint_init', {'root': str(source), 'output': str(output / 'preview-map')})['project_directory']
    def call(name, **kwargs):
        return tools.call('blueprint_' + name, {'project': project, **kwargs})
    claim = call('next', worker='local-validation')
    page = claim['reading_pack']
    while page.get('next_cursor'):
        page = call('pack', task_id=claim['task']['id'], cursor=page['next_cursor'])
    for _ in range(2): call('read', source=claim['sources'][0]['id'], limit=30)
    for _ in range(2):
        try: call('query', table='entities', revision=999999)
        except ValueError: pass
    batch = claim['batch_template']; batch.update(result='partial', reason='Local tool validation only; semantic review remains.')
    call('commit', batch=batch); call('commit', batch=batch)
    tools.close()
    first_report = report(open_project(project))
    (output / 'example-report.json').write_text(json.dumps(first_report, ensure_ascii=False, indent=2), encoding='utf-8')

    measured = AgentTools(); measured.set_client_info({'name': 'Local timing overhead benchmark'})
    benchmark = measured.call('blueprint_init', {'root': str(source), 'output': str(output / 'benchmark-map')})['project_directory']
    plain = AgentTools(); plain.metrics = Unrecorded()
    read_args = {'project': benchmark, 'source': 'core.py', 'start': 1, 'limit': 80}
    before = open_project(benchmark).read()
    for reader in (measured, plain): reader.call('blueprint_read', read_args)
    times = {'recorded_ms': [], 'unrecorded_ms': []}
    for round in range(5):
        readers = [('recorded_ms', measured), ('unrecorded_ms', plain)]
        for name, reader in readers if round % 2 == 0 else reversed(readers):
            started = perf_counter()
            for _ in range(20): reader.call('blueprint_read', read_args)
            times[name].append((perf_counter() - started) * 1000)
    measured.close(); plain.close()
    assert before == open_project(benchmark).read(), 'Benchmark changed graph state'
    result = {'scope': 'Local source reads only; no AI, IPC, semantic review or OpenGL/WB end-to-end measurement.',
              'python': platform.python_version(), 'os': platform.system(), 'calls_per_round': 20,
              'rounds': times, 'recorded_median_ms': median(times['recorded_ms']),
              'unrecorded_median_ms': median(times['unrecorded_ms']),
              'added_ms_per_call': (median(times['recorded_ms']) - median(times['unrecorded_ms'])) / 20,
              'graph_unchanged': True, 'preview_map': project}
    (output / 'benchmark.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
