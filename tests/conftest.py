
import datetime
import inspect
import json
import os
import re
import sys
import timeit
from functools import partial

import click
import poyo
import pytest
import runez
import strictyaml
import yaml as pyyaml
from ruamel.yaml import YAML as RYAML

import zyaml


TESTS_FOLDER = os.path.abspath(os.path.dirname(__file__))
SAMPLE_FOLDER = os.path.join(TESTS_FOLDER, "samples")
IMPLEMENTATIONS = []


def get_implementations(name):
    result = []
    for impl in IMPLEMENTATIONS:
        if name in impl.name:
            result.append(impl)
    return result


def ignored_dirs(names):
    for name in names:
        if name.startswith("."):
            yield Sample(name)


def scan_samples(sample_name):
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
                if sample_name == "all" or sample_name in sample.name:
                    yield sample


def get_samples(sample_name):
    return sorted(scan_samples(sample_name), key=lambda x: x.key)


@pytest.fixture
def spec_samples():
    return get_samples("spec")


def as_is(value):
    return value


def json_sanitized(value, stringify=as_is):
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        return [json_sanitized(v) for v in value]
    if isinstance(value, dict):
        return dict((str(k), json_sanitized(v)) for k, v in value.items())
    if isinstance(value, datetime.date):
        return str(value)
    if not isinstance(value, (int, str, float)):
        return stringify(value)
    return stringify(value)


class SingleBenchmark:
    def __init__(self, sample, implementations):
        self.implementations = implementations
        self.sample = sample
        self.fastest = None
        self.seconds = {}
        self.outcome = {}
        self.iterations = 100

    def add(self, name, seconds, message=None):
        if seconds is None:
            if not message:
                message = "failed"
            else:
                message = message.strip().replace("\n", " ")
                message = re.sub(r"\s+", " ", message)
                message = "failed: %s..." % message[:180]
            self.outcome[name] = message
            return
        if self.fastest is None or self.fastest > seconds:
            self.fastest = seconds
        self.seconds[name] = seconds

    def run(self):
        for impl in self.implementations:
            try:
                t = timeit.Timer(stmt=partial(impl.load, self.sample))
                self.add(impl.name, t.timeit(self.iterations))

            except Exception as e:
                self.add(impl.name, None, message="failed %s" % e)

        for name, seconds in self.seconds.items():
            info = "" if seconds == self.fastest else " [x %.1f]" % (seconds / self.fastest)
            self.outcome[name] = "%.3fs%s" % (seconds, info)

    def report(self):
        result = ["%s:" % self.sample]
        for name, outcome in sorted(self.outcome.items()):
            result.append("  %s: %s" % (name, outcome))
        return "\n".join(result)


def implementations_option(option=True, **kwargs):
    def _callback(_ctx, _param, value):
        names = [s.strip() for s in value.split(",")]
        names = [s for s in names if s]
        result = []
        for name in names:
            impl = get_implementations(name)
            if not impl:
                raise click.BadParameter("Unknown implementation %s" % name)
            result.extend(impl)
        return result

    kwargs.setdefault("default", "zyaml,ruamel")

    if option:
        kwargs.setdefault("help", "Implementation(s) to use")
        return click.option("--implementations", "-i", callback=_callback, **kwargs)

    return click.argument("implementations", callback=_callback, **kwargs)


def samples_arg(option=False, **kwargs):
    def _callback(_ctx, _param, value):
        return get_samples(value)

    kwargs.setdefault("default", "spec")

    if option:
        kwargs.setdefault("help", "Sample(s) to use")
        return click.option("--samples", "-s", callback=_callback, **kwargs)

    return click.argument("samples", callback=_callback, **kwargs)


@runez.click.group()
@runez.click.debug()
@runez.click.log()
def main(debug, log):
    """Troubleshooting commands, useful for iterating on this library"""
    runez.log.setup(debug=debug, file_location=log, locations=None)


@main.command()
@implementations_option()
@samples_arg()
def benchmark(implementations, samples):
    """Run parsing benchmarks"""
    for sample in samples:
        bench = SingleBenchmark(sample, implementations)
        bench.run()
        print(bench.report())


@main.command()
@implementations_option()
@samples_arg()
def diff(implementations, samples):
    """Compare deserialization of 2 implementations"""
    if len(implementations) != 2:
        sys.exit("Need exactly 2 implementations to compare")

    for sample in samples:
        r1 = implementations[0].load(sample)
        r2 = implementations[1].load(sample)
        print("%s %s" % (r1.diff(r2), sample))


@main.command()
@samples_arg()
def find_samples(samples):
    """Show which samples match given filter"""
    print("\n".join(str(s) for s in samples))


@main.command()
@click.option("--stacktrace", help="Show stacktrace on failure")
@implementations_option()
@samples_arg(default="misc.yml")
def show(stacktrace, implementations, samples):
    """Show deserialized yaml objects as json"""
    for sample in samples:
        report = []
        values = set()
        for impl in implementations:
            result = impl.load(sample, stacktrace=stacktrace)
            result.wrap = json_representation
            rep = str(result)
            values.add(rep)
            report.append("-- %s:\n%s" % (impl, rep))
        print("==== %s (match: %s):" % (sample, len(values) == 1))
        print("\n".join(report))
        print()


@main.command()
@samples_arg()
def refresh(samples):
    """Refresh expected json for each sample"""
    for sample in samples:
        sample.refresh()


@main.command()
@implementations_option(default="zyaml,pyyaml_base")
@samples_arg(default="misc.yml")
def tokens(implementations, samples):
    """Refresh expected json for each sample"""
    for sample in samples:
        print("==== %s:" % sample)
        for impl in implementations:
            print("\n-- %s tokens:" % impl)
            for t in impl.tokens(sample):
                print(t)
            print()


class Sample(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self.basename = os.path.basename(self.path)
        self.folder = os.path.dirname(self.path)
        if self.path.startswith(SAMPLE_FOLDER):
            self.name = self.path[len(SAMPLE_FOLDER) + 1:]
        else:
            self.name = self.path
        self.key = self.name if "/" in self.name else "./%s" % self.name
        self._expected = None

    def __repr__(self):
        return self.name

    @property
    def expected_path(self):
        return os.path.join(self.folder, "expected", self.basename.replace(".yml", ".json"))

    @property
    def expected(self):
        if self._expected is None:
            try:
                with open(self.expected_path) as fh:
                    self._expected = json.load(fh)
            except (OSError, IOError):
                return None
        return self._expected

    def refresh(self, impl=None):
        """
        :param YmlImplementation impl: Implementation to use
        """
        if impl is None:
            impl = RuamelImplementation()
        rep = impl.json_representation(self, stringify=as_is)
        with open(self.expected_path, "w") as fh:
            fh.write(rep)


class ParseResult(object):
    def __init__(self, impl, sample, data=None):
        self.impl = impl  # type: YmlImplementation
        self.wrap = str
        self.sample = sample
        self.data = data
        self.exception = None
        self.error = None

    def __repr__(self):
        if self.error:
            return "error: %s" % self.error
        return self.wrap(self.data)

    def diff(self, other):
        if self.error or other.error:
            if self.error and other.error:
                return "invalid"
            return "%s %s  " % ("F " if self.error else "  ", "F " if other.error else "  ")
        if self.data == other.data:
            return "match  "
        return "diff   "


class YmlImplementation(object):
    """Implementation of loading a yml file"""

    def __repr__(self):
        return self.name

    @property
    def name(self):
        return "_".join(s.lower() for s in re.findall("[A-Z][^A-Z]*", self.__class__.__name__.replace("Implementation", "")))

    def _load(self, stream):
        return []

    def tokens(self, sample, comments=True):
        try:
            with open(sample.path) as fh:
                for t in self._tokens(fh.read(), comments):
                    yield t
        except Exception as e:
            yield "--> can't get tokens: %s" % e

    def _tokens(self, contents, comments):
        raise Exception("not implemented")

    def load_stream(self, contents):
        data = self._load(contents)
        if data is not None and inspect.isgenerator(data):
            data = list(data)
        return zyaml.simplified(data)

    def load_path(self, path):
        with open(path) as fh:
            return self.load_stream(fh)

    def load(self, sample, stacktrace=None):
        if stacktrace is None:
            # By default, show stacktrace when running in pycharm
            stacktrace = "PYCHARM_HOSTED" in os.environ

        if stacktrace:
            return ParseResult(self, sample, self.load_path(sample.path))

        result = ParseResult(self, sample)
        try:
            result.data = self.load_path(sample.path)
        except Exception as e:
            result.exception = e
            result.error = str(e)
        return result

    def json_representation(self, sample, stringify=as_is):
        result = self.load(sample)
        return json_representation(result.data, stringify=stringify)


def json_representation(data, stringify=as_is):
    data = json_sanitized(data, stringify=stringify)
    return "%s\n" % json.dumps(data, sort_keys=True, indent=2)


class ZyamlImplementation(YmlImplementation):
    def _load(self, stream):
        return zyaml.load_string(stream.read())

    def _tokens(self, stream, comments):
        settings = zyaml.ScanSettings(yield_comments=comments)
        return zyaml.scan_tokens(stream, settings=settings)


class RuamelImplementation(YmlImplementation):
    def _load(self, stream):
        y = RYAML(typ="safe")
        y.constructor.yaml_constructors["tag:yaml.org,2002:timestamp"] = y.constructor.yaml_constructors["tag:yaml.org,2002:str"]
        return y.load_all(stream)


class PyyamlBaseImplementation(YmlImplementation):
    def _load(self, stream):
        return pyyaml.load_all(stream, Loader=pyyaml.BaseLoader)

    def _tokens(self, stream, comments):
        yaml_loader = pyyaml.BaseLoader(stream)
        curr = yaml_loader.get_token()
        while curr is not None:
            yield curr
            nxt = yaml_loader.get_token()
            if comments:
                for comment in self._comments_between_tokens(curr, nxt):
                    yield comment
            curr = nxt

    @staticmethod
    def _comments_between_tokens(token1, token2):
        """Find all comments between two tokens"""
        if token2 is None:
            buf = token1.end_mark.buffer[token1.end_mark.pointer:]
        elif (token1.end_mark.line == token2.start_mark.line and
              not isinstance(token1, pyyaml.StreamStartToken) and
              not isinstance(token2, pyyaml.StreamEndToken)):
            return
        else:
            buf = token1.end_mark.buffer[token1.end_mark.pointer:token2.start_mark.pointer]
        for line in buf.split('\n'):
            pos = line.find('#')
            if pos != -1:
                yield zyaml.CommentToken(token1.end_mark.line, token1.end_mark.column, line[pos:])


class PoyoImplementation(YmlImplementation):
    def _load(self, stream):
        return [poyo.parse_string(stream.read())]


class StrictImplementation(YmlImplementation):
    def _load(self, stream):
        return strictyaml.load(stream.read())


for i in YmlImplementation.__subclasses__():
    IMPLEMENTATIONS.append(i())


if __name__ == "__main__":
    main()
