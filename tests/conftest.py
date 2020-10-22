# -*- encoding: utf-8 -*-
import codecs
import datetime
import inspect
import json
import os
import re
import sys
import timeit
from contextlib import contextmanager
from functools import partial

import click
import poyo
import pytest
import ruamel.yaml
import runez
import strictyaml
import yaml as pyyaml

import zyaml
from zyaml.marshal import *


TESTS_FOLDER = os.path.abspath(os.path.dirname(__file__))
PROJECT_FOLDER = os.path.dirname(TESTS_FOLDER)
SAMPLE_FOLDER = os.path.join(TESTS_FOLDER, "samples")
UNDEFINED = object()


def relative_sample_path(path, base=SAMPLE_FOLDER):
    if path and path.startswith(base):
        return path[len(base) + 1:]
    return path


def ignored_dirs(names):
    for name in names:
        if name.startswith("."):
            yield Sample(name)


def get_descendants(ancestor, adjust=None, _result=None):
    if _result is None:
        _result = {}
    for m in ancestor.__subclasses__():
        name = m.__name__
        if adjust is not None:
            name = adjust(name)
        _result[name] = m
        get_descendants(m, adjust=adjust, _result=_result)
    return _result


def scan_samples(sample_name):
    sample_name = sample_name.strip()
    if not sample_name:
        return
    if os.path.isfile(sample_name) or os.path.isabs(sample_name):
        yield Sample(sample_name)
        return

    folder = SAMPLE_FOLDER
    if os.path.isdir(sample_name):
        folder = sample_name
        sample_name = "all"

    for root, dirs, files in os.walk(folder):
        for dir_name in list(ignored_dirs(dirs)):
            dirs.remove(dir_name)
        for fname in files:
            if fname.endswith(".yml"):
                sample = Sample(os.path.join(root, fname))
                if sample.is_match(sample_name):
                    yield sample


def get_samples(sample_name):
    result = []
    for name in runez.flattened([sample_name], split=","):
        result.extend(scan_samples(name))
    return sorted(result, key=lambda x: x.key)


TESTED_SAMPLES = "flex,invalid,minor,valid"


@pytest.fixture
def all_samples():
    return get_samples(TESTED_SAMPLES)


def json_sanitized(value, stringify=zyaml.decode, dt=str):
    if value is None:
        return None
    if isinstance(value, set):
        return [json_sanitized(v, stringify=stringify, dt=dt) for v in sorted(value)]
    if isinstance(value, (tuple, list)):
        return [json_sanitized(v, stringify=stringify, dt=dt) for v in value]
    if isinstance(value, dict):
        return dict((str(k), json_sanitized(v, stringify=stringify, dt=dt)) for k, v in value.items())
    if isinstance(value, datetime.date):
        return dt(value)
    if isinstance(value, strictyaml.representation.YAML):
        return dt(value)
    if stringify is None:
        return value
    if not isinstance(value, (int, str, float)):
        return stringify(value)
    return stringify(value)


class BenchmarkedFunction(object):
    def __init__(self, name, function, iterations):
        self.name = name
        self.function = function
        self.iterations = iterations
        self.error = None
        self.seconds = None

    def __repr__(self):
        return self.report()

    def run(self, stacktrace=False):
        t = timeit.Timer(stmt=self.function)
        if stacktrace:
            self.seconds = t.timeit(self.iterations)
            return

        try:
            self.seconds = t.timeit(self.iterations)

        except Exception as e:
            self.error = runez.short(e)

    def report(self, fastest=None, indent=""):
        if self.error:
            return "%s: failed: %s..." % (self.name, runez.short(self.error, size=180))

        if self.seconds is None:
            return self.name

        info = ""
        if fastest and self.seconds and fastest.seconds and self.seconds != fastest.seconds:
            info = runez.dim(" [x %.1f]" % (self.seconds / fastest.seconds))

        unit = "μ"
        x = self.seconds / self.iterations * 1000000
        if x >= 999:
            x = x / 1000
            unit = "m"

        if x >= 999:
            x = x / 1000
            unit = "s"

        return "%s%s: %.3f %ss/i%s" % (indent, self.name, x, unit, info)


class BenchmarkRunner(object):
    def __init__(self, functions, target_name=None, iterations=100):
        self.benchmarks = []
        for name, func in functions.items():
            self.benchmarks.append(BenchmarkedFunction(name, func, iterations))
        self.target_name = target_name
        self.fastest = None

    def run(self, stacktrace=False):
        for bench in self.benchmarks:
            bench.run(stacktrace=stacktrace)
            if self.fastest is None or self.fastest.seconds > bench.seconds:
                self.fastest = bench

    def report(self):
        result = []
        indent = ""
        if self.target_name:
            indent = "  "
            result.append("%s:" % self.target_name)

        for bench in self.benchmarks:
            result.append(bench.report(fastest=self.fastest, indent=indent))

        return "\n".join(result)


def stacktrace_option():
    return click.option(
        "--stacktrace", "-x",
        default=None, is_flag=True,
        help="Leave exceptions uncaught (to conveniently stop in debugger)"
    )


class ImplementationCollection(object):
    def __init__(self, names, default="zyaml,ruamel"):
        self.available = get_descendants(YmlImplementation, adjust=lambda x: x.replace("Implementation", "").lower())
        self.available = dict((n, i()) for n, i in self.available.items())
        self.unknown = []
        self.selected = []
        if names.startswith("+"):
            names = "%s,%s" % (names[1:], default)
        names = [s.strip() for s in names.split(",")]
        names = [s for s in names if s]
        seen = {}
        for name in names:
            found = 0
            for i in self.available.values():
                if name == "all" or name in i.name:
                    if i.name not in seen:
                        seen[i.name] = True
                        self.selected.append(i)
                    found += 1
            if found == 0:
                self.unknown.append(name)
        self.combinations = None

    def track_result_combination(self, impl, value):
        name = impl.name
        if self.combinations is None:
            self.combinations = {}
            for i1 in self.selected:
                for i2 in self.selected:
                    if i1.name < i2.name:
                        self.combinations[(i1.name, i2.name)] = set()
        for names, values in self.combinations.items():
            if name in names:
                values.add(value)

    def __repr__(self):
        return ",".join(str(i) for i in self.selected)

    def __len__(self):
        return len(self.selected)

    def __iter__(self):
        for i in self.selected:
            yield i


def implementations_option(option=True, default="zyaml,ruamel", count=None, **kwargs):
    """
    :param bool option: If True, make this an option
    :param str default: Default implementation(s) to use
    :param int|None count: Exact number of implementations needed (when applicable)
    :param kwargs: Passed-through to click
    :return ImplementationCollection: Implementations to use
    """
    kwargs["default"] = default

    def _callback(_ctx, _param, value):
        implementations = ImplementationCollection(value, default=default)
        if implementations.unknown:
            raise click.BadParameter("Unknown implementation%s %s" % (plural(len(implementations)), ", ".join(implementations.unknown)))
        if count and len(implementations) != count:
            if count == 1:
                raise click.BadParameter("Need exactly 1 implementation")
            raise click.BadParameter("Need exactly %s implementations" % count)
        if count == 1:
            return implementations.selected[0]
        return implementations

    if option:
        if count and count > 1:
            hlp = "%s implementations to use" % count
        else:
            hlp = "Implementation%s to use" % plural(count)
        kwargs.setdefault("help", hlp)
        kwargs.setdefault("show_default", True)
        kwargs.setdefault("metavar", "IMPL" if count == 1 else "CSV")
        return click.option("--implementation%s" % plural(count), "-i", callback=_callback, **kwargs)

    return click.argument("implementations", callback=_callback, **kwargs)


def plural(count):
    return "s" if count != 1 else ""


def samples_arg(option=False, default=None, count=None, **kwargs):
    metavar = "SAMPLE%s" % plural(count).upper()

    def _callback(_ctx, _param, value):
        if count == 1 and hasattr(value, "endswith") and not value.endswith("."):
            value += "."
        if not value:
            raise click.BadParameter("No filter provided for selecting %s" % metavar)
        s = get_samples(value)
        if not s:
            raise click.BadParameter("No samples match %s" % value)
        if count is not None and 0 < count != len(s):
            raise click.BadParameter("Need exactly %s sample%s, filter yielded %s" % (count, plural(count), len(s)))
        return s

    kwargs["default"] = default
    kwargs.setdefault("metavar", metavar)

    if option:
        kwargs.setdefault("help", "Sample(s) to use")
        kwargs.setdefault("show_default", True)
        return click.option("--samples", "-s", callback=_callback, **kwargs)

    return click.argument("samples", callback=_callback, **kwargs)


@runez.click.group()
@runez.click.debug()
@runez.click.log()
def main(debug, log):
    """Troubleshooting commands, useful for iterating on this library"""
    runez.log.setup(debug=debug, file_location=log, locations=None)


@main.command()
@stacktrace_option()
@implementations_option()
@samples_arg(default="bench")
def benchmark(stacktrace, implementations, samples):
    """Run parsing benchmarks"""
    for sample in samples:
        impls = dict((i.name, partial(i.load, sample)) for i in implementations)
        with runez.Anchored(SAMPLE_FOLDER):
            bench = BenchmarkRunner(impls, target_name=sample.name, iterations=100)
            bench.run(stacktrace)
            print(bench.report())


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
@implementations_option(count=2)
@samples_arg(nargs=-1)
def diff(compact, untyped, tokens, implementations, samples):
    """Compare deserialization of 2 implementations"""
    stringify = str if untyped else zyaml.decode
    if compact is None:
        compact = len(samples) > 1

    with runez.TempFolder():
        generated_files = []
        for sample in samples:
            generated_files.append([sample])
            for impl in implementations:
                assert isinstance(impl, YmlImplementation)
                result = ParseResult(impl, sample)
                try:
                    if tokens:
                        result.data = list(impl.tokens(sample))
                        result.text = "\n".join(impl.represented_token(t) for t in result.data)

                    else:
                        result.data = impl.load_path(sample.path)
                        payload = json_sanitized(result.data, stringify=stringify, dt=simplified_date)
                        result.text = runez.represented_json(payload)

                except Exception as e:
                    result.set_exception(e)
                    result.text = result.error

                fname = "%s-%s.text" % (impl.name, sample.basename)
                generated_files[-1].extend([fname, result])
                if not compact:
                    with open(fname, "w") as fh:
                        fh.write(result.text)
                        if not result.text.endswith("\n"):
                            fh.write("\n")

        matches = 0
        failed = 0
        differ = 0
        for sample, n1, r1, n2, r2 in generated_files:
            if r1.error and r2.error:
                matches += 1
                failed += 1
                print("%s: both failed" % sample)

            elif r1.text == r2.text:
                matches += 1
                print("%s: OK" % sample)

            else:
                differ += 1
                if compact:
                    print("%s: differ" % sample)

        if not compact:
            for sample, n1, r1, n2, r2 in generated_files:
                if r1.text != r2.text:
                    r = runez.run("diff", "-br", "-U1", n1, n2, fatal=None)
                    print("========  %s  ========" % sample)
                    print(r.full_output)
                    print()

        print()
        print("%s samples, %s match, %s differ, %s failed" % (matches + differ, matches, differ, failed))


@main.command()
@samples_arg()
def find_samples(samples):
    """Show which samples match given filter"""
    for s in samples:
        print(s)


def move(source, dest, basename, extension, subfolder=None):
    if os.path.isfile(source):
        if subfolder:
            dest = os.path.join(dest, subfolder)
        dest = os.path.join(dest, basename + extension)
        print("Moving %s -> %s" % (relative_sample_path(source, base=PROJECT_FOLDER), relative_sample_path(dest, base=PROJECT_FOLDER)))
        runez.move(source, dest)


@main.command()
@samples_arg(count=1)
@click.argument("category", nargs=1)
def mv(samples, category):
    """Move a sample to a given category"""
    sample = samples[0]
    if sample.category == category:
        print("%s is already in %s" % (sample, category))
        sys.exit(0)
    dest = os.path.join(SAMPLE_FOLDER, category)
    if not os.path.isdir(dest):
        sys.exit("No folder %s" % relative_sample_path(dest, base=PROJECT_FOLDER))
    move(sample.path, dest, sample.basename, ".yml")
    move(sample.expected_path, dest, sample.basename, ".json", subfolder="_expected")


@main.command(name="print")
@click.option("--tokens", "-t", is_flag=True, help="Show zyaml tokens as well")
@stacktrace_option()
@implementations_option(default="zyaml,ruamel")
@click.argument("text", nargs=-1)
def print_(tokens, stacktrace, implementations, text):
    """Deserialize given argument as yaml"""
    text = " ".join(text)
    text = codecs.decode(text, "unicode_escape")
    print("--- raw:\n%s" % text)
    for impl in implementations:
        assert isinstance(impl, YmlImplementation)
        if tokens and impl.name == "zyaml":
            result = "\n".join(str(s) for s in impl.tokens(text, stacktrace=stacktrace))
            print("---- %s tokens:\n%s" % (impl, result))
        if stacktrace:
            data = impl.load_string(text)
            rtype = data.__class__.__name__ if data is not None else "None"
            result = impl.json_representation(ParseResult(impl, None, data=data))[:-1]
        else:
            try:
                data = impl.load_string(text)
                rtype = data.__class__.__name__ if data is not None else "None"
                result = impl.json_representation(ParseResult(impl, None, data=data))[:-1]
            except Exception as e:
                rtype = "error"
                result = str(e).replace("\n", " ") or e.__class__.__name__
                result = re.sub(r"\s+", " ", result)
                result = runez.short(result)
        print("---- %s: %s\n%s" % (impl, rtype, result))


def _bench1(size):
    return "%s" % size


def _bench2(size):
    return "{}".format(size)


@main.command()
def perfplot():
    """Convenience entry point to perf-plot different function samples"""
    import perfplot

    functions = []
    labels = []
    for name, func in globals().items():
        if name.startswith("_bench"):
            name = name[1:]
            functions.append(func)
            labels.append(name)

    perfplot.show(
        setup=lambda n: n,  # or simply setup=numpy.random.rand
        kernels=functions,
        labels=labels,
        n_range=[2 ** k for k in range(15)],
        xlabel="len(a)",
        equality_check=None,
        target_time_per_measurement=5.0,
        time_unit="us",  # set to one of ("auto", "s", "ms", "us", or "ns") to force plot units
    )


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
    bench.run(stacktrace=True)
    print(bench.report())


@main.command()
@stacktrace_option()
@implementations_option(count=1, default="zyaml")
@samples_arg(default=TESTED_SAMPLES)
def refresh(stacktrace, implementation, samples):
    """Refresh expected json for each sample"""
    for root, dirs, files in os.walk(SAMPLE_FOLDER):
        if root.endswith("_expected"):
            for fname in files:
                ypath = os.path.dirname(root)
                ypath = os.path.join(ypath, fname.replace(".json", ".yml"))
                if not os.path.isfile(ypath):
                    # Delete _expected json files for yml files that have been moved
                    jpath = os.path.join(root, fname)
                    print("Deleting %s" % relative_sample_path(jpath))
                    os.unlink(jpath)

    for sample in samples:
        sample.refresh(impl=implementation, stacktrace=stacktrace)


@contextmanager
def profiled(enabled):
    if not enabled:
        yield False
        return
    import cProfile
    profiler = cProfile.Profile()
    try:
        profiler.enable()
        yield True
    finally:
        profiler.disable()
        filepath = os.path.join(PROJECT_FOLDER, ".tox", "lastrun.profile")
        try:
            profiler.dump_stats(filepath)
            if runez.which("qcachegrind") is None:
                print("run 'brew install qcachegrind'")
                return
            runez.run("pyprof2calltree", "-k", "-i", filepath, stdout=None, stderr=None)
        except Exception as e:
            print("Can't save %s: %s" % (filepath, e))


@main.command()
@click.option("--profile", is_flag=True, help="Enable profiling")
@click.option("--tokens", "-t", is_flag=True, help="Show zyaml tokens as well")
@click.option("--line-numbers", "-n", is_flag=True, help="Show line numbers when showing original yaml")
@stacktrace_option()
@implementations_option(default="zyaml,ruamel")
@samples_arg(default="misc")
def show(profile, tokens, line_numbers, stacktrace, implementations, samples):
    """Show deserialized yaml objects as json"""
    with profiled(profile) as is_profiling:
        for sample in samples:
            print("========  %s  ========" % sample)
            with open(sample.path) as fh:
                if line_numbers:
                    print("".join("%4s: %s" % (n + 1, s) for n, s in enumerate(fh.readlines())))
                else:
                    print("".join(fh.readlines()))
            assert isinstance(implementations, ImplementationCollection)
            for impl in implementations:
                if tokens and impl.name == "zyaml":
                    result = "\n".join(str(s) for s in impl.tokens(sample, stacktrace=stacktrace))
                    print("---- %s tokens:\n%s" % (impl, result))
                print("--------  %s  --------" % impl)
                assert isinstance(impl, YmlImplementation)
                result = impl.load(sample, stacktrace=stacktrace)
                if is_profiling:
                    return
                if result.error:
                    rep = "Error: %s\n" % result.error
                    implementations.track_result_combination(impl, "error")
                else:
                    rep = impl.json_representation(result)
                    implementations.track_result_combination(impl, rep)
                print(rep)
            if implementations.combinations:
                combinations = ["/".join(x) for x in implementations.combinations]
                fmt = "-- %%%ss %%s" % max(len(s) for s in combinations)
                for names, values in implementations.combinations.items():
                    print(fmt % ("/".join(names), "matches" if len(values) == 1 else "differ"))
                print()


@main.command()
@stacktrace_option()
@implementations_option(default="zyaml,pyyaml_base")
@samples_arg(default="misc")
def tokens(stacktrace, implementations, samples):
    """Show tokens for given samples"""
    for sample in samples:
        print("========  %s  ========" % sample)
        with open(sample.path) as fh:
            print("".join(fh.readlines()))
        for impl in implementations:
            print("--------  %s  --------" % impl)
            for t in impl.tokens(sample, stacktrace=stacktrace):
                print(impl.represented_token(t))
            print()


class Sample(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.basename = os.path.basename(self.path)
        self.basename, _, self.extension = self.basename.rpartition(os.path.extsep)
        self.folder = os.path.dirname(self.path)
        self.name = relative_sample_path(self.path)
        self.category = os.path.dirname(self.name)
        self.key = self.name if "/" in self.name else "./%s" % self.name
        self._expected = None

    def __repr__(self):
        return self.name

    @property
    def expected_path(self):
        return os.path.join(self.folder, "_expected", "%s.json" % self.basename)

    @property
    def expected(self):
        if self._expected is None:
            try:
                with open(self.expected_path) as fh:
                    self._expected = json.load(fh)
            except (OSError, IOError):
                return UNDEFINED
        return self._expected

    def is_match(self, name):
        if name == "all":
            return True
        if name.endswith("."):  # Special case when looking for exactly 1 sample
            if name[:-1] == self.basename:
                return True
            if self.basename.endswith(name[:-1]) and len(self.basename) > len(name):
                return self.basename[-len(name)] == "-"
            return False
        if self.category.startswith(name):
            return True
        if self.basename.startswith(name) or self.basename.endswith(name):
            return True

    def refresh(self, impl, stacktrace=None):
        """
        :param YmlImplementation impl: Implementation to use
        :param bool stacktrace: If True, don't catch parsing exceptions
        """
        result = impl.load(self, stacktrace=stacktrace)
        rep = impl.json_representation(result)
        folder = os.path.dirname(self.expected_path)
        if not os.path.isdir(folder):
            os.mkdir(folder)
        with open(self.expected_path, "w") as fh:
            fh.write(rep)


class ParseResult(object):
    def __init__(self, impl, sample, data=None):
        self.impl = impl  # type: YmlImplementation
        self.sample = sample
        self.data = data
        self.exception = None
        self.error = None

    def __repr__(self):
        if self.error:
            return "Error: %s" % self.error
        return str(self.data)

    def set_exception(self, exc):
        self.exception = exc
        self.error = runez.short(exc, size=160)
        if not self.error:
            self.error = exc.__class__.__name__

    def json_payload(self):
        return {"_error": self.error} if self.error else self.data


class YmlImplementation(object):
    """Implementation of loading a yml file"""

    def __repr__(self):
        return self.name

    @property
    def name(self):
        return "_".join(s.lower() for s in re.findall("[A-Z][^A-Z]*", self.__class__.__name__.replace("Implementation", "")))

    def _load(self, stream):
        return []

    def tokens(self, sample, stacktrace=False):
        if isinstance(sample, Sample):
            with open(sample.path) as fh:
                contents = fh.read()
        else:
            contents = sample

        if stacktrace:
            for t in self._tokens(contents):
                yield t
            return

        try:
            for t in self._tokens(contents):
                yield t
        except Exception as e:
            yield "Error: %s" % e

    def _tokens(self, contents):
        raise Exception("not implemented")

    def _simplified(self, value):
        if isinstance(value, list) and len(value) == 1:
            return value[0]
        return value

    def load_string(self, contents):
        data = self._load(contents)
        if data is not None and inspect.isgenerator(data):
            data = list(data)
        return self._simplified(data)

    def load_path(self, path):
        with open(path) as fh:
            return self.load_string(fh.read())

    def load(self, sample, stacktrace=True):
        """
        :param Sample sample: Sample to load
        :param bool stacktrace: If True, don't catch parsing exceptions
        :return ParseResult: Parsed sample
        """
        if stacktrace:
            return ParseResult(self, sample, self.load_path(sample.path))

        result = ParseResult(self, sample)
        try:
            result.data = self.load_path(sample.path)

        except Exception as e:
            result.set_exception(e)

        return result

    def json_representation(self, result, stringify=zyaml.decode):
        try:
            payload = result.json_payload()
            payload = json_sanitized(payload, stringify=stringify)
            return "%s\n" % json.dumps(payload, sort_keys=True, indent=2)
        except Exception:
            print("Failed to json serialize %s" % result.sample)
            raise

    def represented_token(self, token):
        return str(token)


class ZyamlImplementation(YmlImplementation):
    def _load(self, stream):
        return zyaml.load(stream)

    def _tokens(self, stream):
        return zyaml.Scanner(stream).tokens()

    def _simplified(self, value):
        return value


def ruamel_passthrough_tags(loader, tag, node):
    name = node.__class__.__name__
    if "Seq" in name:
        result = []
        for v in node.value:
            result.append(ruamel_passthrough_tags(loader, tag, v))
        return result
    if "Map" in name:
        result = {}
        for k, v in node.value:
            k = ruamel_passthrough_tags(loader, tag, k)
            v = ruamel_passthrough_tags(loader, tag, v)
            result[k] = v
        return result
    return zyaml.default_marshal(node.value)


class RuamelImplementation(YmlImplementation):
    def _simplified(self, value):
        if not value:
            return None
        if len(value) == 1:
            return value[0]
        return value

    def _load(self, stream):
        y = ruamel.yaml.YAML(typ="safe")
        ruamel.yaml.add_multi_constructor('', ruamel_passthrough_tags, Loader=ruamel.yaml.SafeLoader)
        return y.load_all(stream)


class PyyamlBaseImplementation(YmlImplementation):
    def _load(self, stream):
        return pyyaml.load_all(stream, Loader=pyyaml.BaseLoader)

    def represented_token(self, token):
        linenum = token.start_mark.line + 1
        column = token.start_mark.column + 1
        result = "%s[%s,%s]" % (token.__class__.__name__, linenum, column)
        value = getattr(token, "value", None)
        if value is not None:
            if token.id == "<scalar>":
                value = represented_scalar(token.style, value)

            elif token.id == "<anchor>":
                value = "&%s" % value

            elif token.id == "<alias>":
                value = "*%s" % value

            elif token.id == "<tag>":
                assert isinstance(value, tuple)
                value = "".join(value)

            else:
                assert False

            result = "%s %s" % (result, value)

        return result

    def _tokens(self, stream):
        yaml_loader = pyyaml.BaseLoader(stream)
        curr = yaml_loader.get_token()
        while curr is not None:
            yield curr
            curr = yaml_loader.get_token()


class PyyamlSafeImplementation(YmlImplementation):
    def _load(self, stream):
        return pyyaml.load_all(stream, Loader=pyyaml.SafeLoader)


class PyyamlFullImplementation(YmlImplementation):
    def _load(self, stream):
        return pyyaml.load_all(stream, Loader=pyyaml.FullLoader)


class PoyoImplementation(YmlImplementation):
    def _load(self, stream):
        return [poyo.parse_string(stream.read())]


class StrictImplementation(YmlImplementation):
    def _load(self, stream):
        return strictyaml.load(stream.read())


if __name__ == "__main__":
    main()
