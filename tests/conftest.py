import codecs
import datetime
import logging
import os
import sys
from functools import partial

import click
import pytest
import runez

from zyaml.marshal import decode

from . import TestSettings
from .benchmark import BenchmarkRunner
from .model import TestSamples
from .ximpl import Implementation


TESTED_SAMPLES = "flex,invalid,misc,valid"


@pytest.fixture
def all_samples():
    return TestSamples.get_samples(TESTED_SAMPLES)


@runez.click.group()
@runez.click.color()
@runez.click.debug()
@runez.click.dryrun()
@runez.click.log()
@click.option("--lines", "-l", default=None, is_flag=True, help="Show line numbers")
@click.option("--profile", is_flag=True, help="Enable profiling")
@click.option("--stacktrace", "-x", default=None, is_flag=True, help="Leave exceptions uncaught (to conveniently stop in debugger)")
def main(debug, log, lines, profile, stacktrace):
    """Troubleshooting commands, useful for iterating on this library"""
    TestSettings.line_numbers = lines
    TestSettings.stacktrace = stacktrace
    runez.log.setup(debug=debug, console_level=logging.INFO, file_location=log, locations=None)
    logging.debug("Running with %s, v%s", runez.short(sys.executable), ".".join(str(s) for s in sys.version_info[:3]))
    runez.Anchored.add([runez.DEV.project_folder, TestSamples.SAMPLE_FOLDER])
    if profile:
        import atexit
        import cProfile

        TestSettings.profiler = cProfile.Profile()
        TestSettings.profiler.enable()
        atexit.register(TestSettings.stop_profiler)


@main.command()
@click.option("--iterations", "-n", default=100, help="Number of iterations to average")
@click.option("--tokens", "-t", is_flag=True, help="Tokenize only")
@Implementation.option()
@TestSamples.option(default="bench")
def benchmark(iterations, tokens, implementations, samples):
    """Compare parsing speed of same file across yaml implementations"""
    for sample in samples:
        if tokens:
            impls = dict((i.name, partial(i.tokens, sample)) for i in implementations)

        else:
            impls = dict((i.name, partial(i.deserialized, sample)) for i in implementations)

        bench = BenchmarkRunner(impls, target_name=sample.name, iterations=iterations)
        bench.run()
        print(bench.report())


@main.command()
def clean():
    """Clean tests/samples/, remove left-over old baselines"""
    TestSamples.clean_samples(verbose=True)


def simplified_date(value):
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            if value.tzinfo != datetime.timezone.utc:  # Get back to ruamel-like flawed time-zoning
                value = value.astimezone(datetime.timezone.utc)

            value = value.replace(tzinfo=None)

    return str(value)


@main.command()
@click.option("--compact/--no-compact", "-1", is_flag=True, default=None, help="Do not show diff text")
@click.option("--untyped", "-u", is_flag=True, help="Parse everything as strings")
@click.option("--tokens", "-t", is_flag=True, help="Compare tokens")
@Implementation.option(count=2)
@TestSamples.option()
def diff(compact, untyped, tokens, implementations, samples):
    """Compare deserialization of 2 implementations"""
    stringify = runez.stringified if untyped else decode
    if compact is None:
        compact = len(samples) > 1

    with runez.TempFolder():
        generated_files = []
        for sample in samples:
            generated_files.append([sample])
            for impl in implementations:
                assert isinstance(impl, Implementation)
                data = impl.get_outcome(sample, tokens=tokens)
                rep = TestSettings.represented(data, size=None, stringify=stringify, dt=simplified_date)
                fname = "%s-%s.txt" % (impl.name, sample.basename)
                generated_files[-1].extend([fname, rep])
                if not compact:
                    with open(fname, "w") as fh:
                        fh.write(rep)
                        if not rep.endswith("\n"):
                            fh.write("\n")

        matches = 0
        failed = 0
        differ = 0
        for sample, n1, r1, n2, r2 in generated_files:
            if isinstance(r1, dict) and isinstance(r2, dict) and r1.get("_error") and r2.get("_error"):
                matches += 1
                failed += 1
                print("%s: both failed" % sample)

            elif r1 == r2:
                matches += 1
                print("%s: OK" % sample)

            else:
                differ += 1
                if compact:
                    print("%s: differ" % sample)

        if not compact:
            for sample, n1, r1, n2, r2 in generated_files:
                if r1 != r2:
                    r = runez.run("diff", "-br", "-U1", n1, n2, fatal=None)
                    print("========  %s  ========" % sample)
                    print(r.full_output)
                    print()

        message = [
            runez.plural(samples, "sample"),
            TestSettings.colored_if_meaningful(matches, "match", runez.green),
            TestSettings.colored_if_meaningful(differ, "differ", runez.orange),
            TestSettings.colored_if_meaningful(failed, "failed", runez.red),
        ]
        print("\n%s" % ", ".join(message))


@main.command()
@TestSamples.option()
def find(samples):
    """Show which samples match given filter"""
    for s in samples:
        print(s)


@main.command()
@TestSamples.option(count=1)
@click.argument("target", nargs=1)
def mv(sample, target):
    """Move a test sample (and its baseline) to a new place"""
    new_category, _, new_basename = target.partition("/")
    if "/" in new_basename:
        sys.exit("Invalid target '%s': use at most one / separator" % runez.red(target))

    if not new_basename:
        new_basename = sample.basename

    if new_basename.endswith(".yml"):
        new_basename = new_basename[:-4]

    old_source = os.path.join(sample.category, sample.basename)
    new_target = os.path.join(new_category, new_basename)
    if old_source == new_target:
        print("%s is already in the right spot" % runez.bold(sample))
        sys.exit(0)

    existing = TestSamples.get_samples(new_target + ".yml")
    if existing:
        sys.exit("There is already a sample '%s'" % runez.red(new_target))

    TestSamples.move_sample_file(sample, new_category, new_basename)
    TestSamples.move_sample_file(sample, new_category, new_basename, kind=TestSamples.K_DESERIALIZED)
    TestSamples.move_sample_file(sample, new_category, new_basename, kind=TestSamples.K_TOKEN)
    TestSamples.clean_samples()


def show_outcome(content, implementations, tokens=False):
    TestSettings.show_lines(content)
    for impl in implementations:
        assert isinstance(impl, Implementation)
        data = impl.get_outcome(content, tokens=tokens)
        impl.show_result(data, tokens=tokens)
        if TestSettings.profiler:
            return

        if not tokens:
            implementations.track_result_combination(impl, data)

    if implementations.combinations:
        matches = []
        differs = []
        for names, values in implementations.combinations.items():
            combination = " / ".join(sorted(names, reverse=True))
            if len(values) == 1:
                matches.append(combination)

            else:
                differs.append(combination)

        if matches:
            print("-- matches: %s" % runez.green(", ".join(sorted(matches, reverse=True))))

        if differs:
            print("-- differs: %s" % runez.red(", ".join(sorted(differs, reverse=True))))


@main.command()
@click.option("--target", "-t", default="_tmp/names.tsv", help="Where to save result")
@click.argument("name", nargs=-1)
def name_available(target, name):
    """Check whether a name is taken or not (.com, .org, .dev and github"""
    import datetime
    import socket
    import requests

    now = datetime.datetime.now()
    now = now.strftime("%Y-%m-%d")
    names = runez.flattened(name)
    domains = "com org dev io net".split()
    k_github = "github"
    k_pypi = "pypi"
    k_available = "----"
    k_taken = "taken"

    report = {}
    existing = {}
    has_header = False
    if os.path.exists(target):
        with open(target) as fh:
            for line in fh:
                if line.startswith("Name"):
                    has_header = True
                    continue

                line = line.rstrip("\n")
                name, _, _ = line.partition("\t")
                name = name.strip()
                existing[name] = line

    if names == ["_redo"]:
        has_header = False
        names = sorted(existing)
        existing = {}

    for name in names:
        if name in existing:
            print("Skipping '%s': already queried %s" % (name, existing[name]))
            continue

        report[name] = "%-8s\t%-10s" % (name, now)
        r = requests.head("https://github.com/%s" % name)
        report[name] += "\t%s" % (k_available if r.status_code >= 400 else k_taken)
        r = requests.head("https://pypi.org/project/%s/" % name)
        report[name] += "\t%s" % (k_available if r.status_code >= 400 else k_taken)
        for domain in domains:
            hostname = "%s.%s" % (name, domain)
            try:
                socket.gethostbyname_ex(hostname)
                status = k_taken

            except socket.gaierror:
                status = k_available

            report[name] += "\t%-4s" % status

    header = ["%-8s" % "Name", "%-10s" % "Checked", k_github, k_pypi]
    header += [".%-4s" % s for s in domains]
    header = "\t".join(header)
    header = "%s\n" % header.strip()
    if not has_header:
        with open(target, "w") as fh:
            fh.write(header)

    if report:
        print(header.strip())
        with open(target, "a") as fh:
            for name, line in sorted(report.items()):
                fh.write(line)
                fh.write("\n")
                print(line)

        print("")

    print("%s names added" % len(report))


@main.command(name="print")
@click.option("--tokens", "-t", is_flag=True, help="Show tokens")
@Implementation.option()
@click.argument("text", nargs=-1, metavar="TEXT")
def print_(tokens, implementations, text):
    """Deserialize given argument as yaml"""
    text = " ".join(text)
    text = codecs.decode(text, "unicode_escape")
    show_outcome(text, implementations, tokens=tokens)


@main.command()
@click.option("--iterations", "-i", default=100, help="Number of iterations to run")
@click.option("--size", "-s", default=100000, help="Simulated size of each iteration")
def quick_bench(iterations, size):
    """Convenience entry point to time different function samples"""
    functions = {}
    for name, func in globals().items():
        if name.startswith("_bench"):
            name = name[1:]
            functions[name] = partial(func, size)

    bench = BenchmarkRunner(functions, iterations=iterations)
    bench.run()
    print(bench.report())


@main.command()
@click.option("--existing", is_flag=True, help="Refresh existing case only")
@click.option("--tokens", "-t", is_flag=True, help="Refresh tokens only")
@TestSamples.option(default=TESTED_SAMPLES)
def refresh(existing, tokens, samples):
    """Refresh expected baseline for each test sample"""
    kinds = []
    if tokens:
        kinds.append(TestSamples.K_TOKEN)

    TestSamples.clean_samples()
    for sample in samples:
        sample.refresh(*kinds, existing=existing)


@main.command()
@click.option("--existing", is_flag=True, help="Replay existing case only")
@click.option("--tokens", "-t", is_flag=True, help="Replay tokens only")
@TestSamples.option(default=TESTED_SAMPLES)
def replay(existing, tokens, samples):
    """Rerun samples and compare them with their current baseline"""
    kinds = []
    if tokens:
        kinds.append(TestSamples.K_TOKEN)

    skipped = 0
    for sample in samples:
        report = sample.replay(*kinds)
        if report is runez.UNSET:
            skipped += 1
            report = None if existing else " %s" % runez.yellow("skipped")

        elif report:
            report = "\n%s" % "\n".join("  %s" % s for s in report.splitlines())

        else:
            report = " %s" % runez.green("OK")

        if report is not None:
            print("** %s:%s" % (runez.bold(sample.name), report))

    if skipped:
        print(runez.dim("-- %s skipped" % runez.plural(skipped, "sample")))


@main.command()
@click.option("--tokens", "-t", is_flag=True, help="Show tokens")
@Implementation.option()
@TestSamples.option()
def show(tokens, implementations, samples):
    """Show deserialized yaml objects as json"""
    for sample in samples:
        show_outcome(sample, implementations, tokens=tokens)


def _bench1(size):
    return "%s" % size


def _bench2(size):
    return "{}".format(size)


if __name__ == "__main__":
    main()
