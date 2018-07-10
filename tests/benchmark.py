import os
import timeit
from functools import partial

from conftest import data_paths, load_poyo, load_pyyaml, load_ruamel, load_zyaml


class BenchResults:
    def __init__(self):
        self.fastest = None
        self.seconds = {}
        self.outcome = {}

    def add(self, name, seconds):
        if seconds is None:
            self.outcome[name] = 'failed'
            return
        if self.fastest is None or self.fastest > seconds:
            self.fastest = seconds
        self.seconds[name] = seconds

    def wrapup(self):
        for name, seconds in self.seconds.items():
            info = "" if seconds == self.fastest else " [x %.1f]" % (seconds / self.fastest)
            self.outcome[name] = "%.3fs%s" % (seconds, info)


def bench_pyyaml(path):
    return load_pyyaml(path)


def bench_poyo(path):
    return load_poyo(path)


def bench_ruamel(path):
    return load_ruamel(path)


def bench_zyaml(path):
    try:
        return load_zyaml(path)
    except Exception as e:
        return None


def run_bench(results, path, iterations, func):
    name = func.__name__
    try:
        t = timeit.Timer(stmt=partial(func, path))
        results.add(name, t.timeit(iterations))

    except Exception:
        results.add(name, None)


def run(iterations=100):
    for path in data_paths():
        print("%s:" % os.path.basename(path))
        results = BenchResults()
        run_bench(results, path, iterations, bench_zyaml)
        run_bench(results, path, iterations, bench_pyyaml)
        run_bench(results, path, iterations, bench_poyo)
        run_bench(results, path, iterations, bench_ruamel)
        results.wrapup()
        for name, outcome in sorted(results.outcome.items()):
            print("  %s: %s" % (name, outcome))
        print()


if __name__ == "__main__":
    run()
