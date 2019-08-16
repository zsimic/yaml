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
PROJECT_FOLDER = os.path.dirname(TESTS_FOLDER)
SAMPLE_FOLDER = os.path.join(TESTS_FOLDER, "samples")
IMPLEMENTATIONS = []


def get_implementations(name):
    if name == "all":
        return IMPLEMENTATIONS
    result = []
    for impl in IMPLEMENTATIONS:
        if name in impl.name:
            result.append(impl)
    return result


def relative_sample_path(path, base=SAMPLE_FOLDER):
    if path and path.startswith(base):
        return path[len(base) + 1:]
    return path


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
def vanilla_samples():
    return get_samples("vanilla")


def decode(value):
    """Python 2/3 friendly decoding of output"""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def json_sanitized(value, stringify=decode):
    if value is None:
        return None
    if isinstance(value, set):
        return [json_sanitized(v) for v in sorted(value)]
    if isinstance(value, (tuple, list)):
        return [json_sanitized(v) for v in value]
    if isinstance(value, dict):
        return dict((str(k), json_sanitized(v)) for k, v in value.items())
    if isinstance(value, datetime.date):
        return str(value)
    if isinstance(value, strictyaml.representation.YAML):
        return str(value)
    if not isinstance(value, (int, str, float)):
        return stringify(value)
    return stringify(value)


class SingleBenchmark(object):
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
                self.add(impl.name, None, message=ParseResult.simplified_error(e, self.sample, size=160))

        for name, seconds in self.seconds.items():
            info = "" if seconds == self.fastest else " [x %.1f]" % (seconds / self.fastest)
            self.outcome[name] = "%.3fs%s" % (seconds, info)

    def report(self):
        result = ["%s:" % self.sample]
        for name, outcome in sorted(self.outcome.items()):
            result.append("  %s: %s" % (name, outcome))
        return "\n".join(result)


def stacktrace_option():
    return click.option(
        "--stacktrace", "-x",
        default=None, is_flag=True,
        help="Leave exceptions uncaught (to conveniently stop in debugger)"
    )


def implementations_option(option=True, default="zyaml,ruamel", count=None, **kwargs):
    """
    :param bool option: If True, make this an option
    :param str default: Default implementation(s) to use
    :param int|None count: Exact number of implementations needed (when applicable)
    :param kwargs: Passed-through to click
    :return list[YmlImplementation]: List of implementations to use
    """
    kwargs["default"] = default

    def _callback(_ctx, _param, value):
        names = [s.strip() for s in value.split(",")]
        names = [s for s in names if s]
        result = []
        for name in names:
            impl = get_implementations(name)
            if not impl:
                raise click.BadParameter("Unknown implementation %s" % name)
            result.extend(impl)
        if count and len(result) != count:
            if count == 1:
                raise click.BadParameter("Need exactly 1 implementation")
            raise click.BadParameter("Need exactly %s implementations" % count)
        return result

    if option:
        if count:
            hlp = "%s implementation%s to use" % (count, plural(count))
        else:
            hlp = "Implementations to use"
        kwargs.setdefault("help", hlp)
        kwargs.setdefault("show_default", True)
        kwargs.setdefault("metavar", "CSV")
        return click.option("--implementations", "-i", callback=_callback, **kwargs)

    return click.argument("implementations", callback=_callback, **kwargs)


def plural(count):
    return "s" if count != 1 else ""


def samples_arg(option=False, default="vanilla", count=None, **kwargs):
    def _callback(_ctx, _param, value):
        if count == 1 and value and not value.endswith("."):
            value += "."

        s = get_samples(value)
        if not s:
            raise click.BadParameter("No samples match %s" % value)
        if count and len(s) != count:
            raise click.BadParameter("Need exactly %s sample%s, filter yielded %s" % (count, plural(count), len(s)))
        return s

    kwargs["default"] = default
    kwargs.setdefault("metavar", "SAMPLE%s" % plural(count).upper())

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
@implementations_option()
@samples_arg()
def benchmark(implementations, samples):
    """Run parsing benchmarks"""
    for sample in samples:
        bench = SingleBenchmark(sample, implementations)
        bench.run()
        print(bench.report())


@main.command()
@stacktrace_option()
@click.option("--compact", "-1", is_flag=True, help="Do not show diff text")
@click.option("--untyped", "-u", is_flag=True, help="Parse everything as strings")
@implementations_option(count=2)
@samples_arg()
def diff(stacktrace, compact, untyped, implementations, samples):
    """Compare deserialization of 2 implementations"""
    stringify = str if untyped else decode
    with runez.TempFolder():
        generated_files = []
        for sample in samples:
            generated_files.append([sample])
            for impl in implementations:
                assert isinstance(impl, YmlImplementation)
                result = impl.load(sample, stacktrace=stacktrace)
                fname = "%s-%s.json" % (impl.name, sample.basename)
                generated_files[-1].extend([fname, result])
                if not compact:
                    with open(fname, "w") as fh:
                        if result.error:
                            fh.write("%s\n" % result.error)
                        else:
                            fh.write(result.json_representation(stringify=stringify))

        for sample, n1, r1, n2, r2 in generated_files:
            if r1.error and r2.error:
                print("-- %s: both failed" % sample)
            elif r1.data == r2.data:
                print("-- %s: OK" % sample)
            elif compact:
                print("-- %s: differ" % sample)

        if not compact:
            for sample, n1, r1, n2, r2 in generated_files:
                if r1.data != r2.data:
                    output = runez.run("diff", "-br", "-U1", n1, n2, fatal=None, include_error=True)
                    print("\n==== diff for %s:\n====" % sample)
                    print(output)
                    print()


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
    dest = os.path.join(SAMPLE_FOLDER, category)
    if not os.path.isdir(dest):
        sys.exit("No folder %s" % relative_sample_path(dest, base=PROJECT_FOLDER))
    move(sample.path, dest, sample.basename, ".yml")
    move(sample.expected_path, dest, sample.basename, ".json", subfolder="_expected")


@main.command()
@stacktrace_option()
@implementations_option()
@samples_arg(default="misc.yml")
def show(stacktrace, implementations, samples):
    """Show deserialized yaml objects as json"""
    for sample in samples:
        report = []
        values = set()
        for impl in implementations:
            assert isinstance(impl, YmlImplementation)
            result = impl.load(sample, stacktrace=stacktrace)
            if result.error:
                rep = "Error: %s\n" % result.error
                values.add("error")
            else:
                rep = result.json_representation()
                values.add(rep)
            report.append("-- %s:\n%s" % (impl, rep))
        print("==== %s (match: %s):" % (sample, len(values) == 1))
        print("\n".join(report))


@main.command()
@stacktrace_option()
@implementations_option(count=1, default="zyaml")
@samples_arg()
def refresh(stacktrace, implementations, samples):
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
        sample.refresh(impl=implementations[0], stacktrace=stacktrace)


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
                return None
        return self._expected

    def refresh(self, impl, stacktrace=None):
        """
        :param YmlImplementation impl: Implementation to use
        :param bool stacktrace: If True, don't catch parsing exceptions
        """
        result = impl.load(self, stacktrace=stacktrace)
        rep = result.json_representation()
        folder = os.path.dirname(self.expected_path)
        if not os.path.isdir(folder):
            os.mkdir(folder)
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

    @staticmethod
    def simplified_error(exc, sample, size=None):
        result = str(exc).replace(sample.path, sample.name)
        if size:
            result = runez.shortened(result, size)
        return result

    def set_exception(self, exc):
        self.exception = exc
        self.error = self.simplified_error(exc, self.sample)

    def diff(self, other):
        if self.error or other.error:
            if self.error and other.error:
                return "invalid"
            return "%s %s  " % ("F " if self.error else "  ", "F " if other.error else "  ")
        if self.data == other.data:
            return "match  "
        return "diff   "

    def json_payload(self):
        return {"_error": self.error} if self.error else json_sanitized(self.data)

    def json_representation(self, stringify=decode):
        try:
            return json_representation(self.json_payload(), stringify=stringify)
        except Exception as e:
            raise Exception("Can't json-serialize %s: %s" % (self.sample, e))


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
            yield "Error: %s" % e

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

    def json_representation(self, sample, stringify=decode):
        result = self.load(sample, stacktrace=False)
        return json_representation(result.data, stringify=stringify)


def json_representation(data, stringify=decode):
    data = json_sanitized(data, stringify=stringify)
    return "%s\n" % json.dumps(data, sort_keys=True, indent=2)


class DummyImplementation(YmlImplementation):
    def _load(self, stream):
        settings = zyaml.ScanSettings()
        settings.buffer = stream.read()
        tokenizer_map = {
            # "%": zyaml.DirectiveTokenizer,
            # "!": zyaml.TagTokenizer,
            # "&": zyaml.TagTokenizer,
            # "*": zyaml.TagTokenizer,
            # ">": zyaml.LiteralTokenizer,
            # "|": zyaml.LiteralTokenizer,
            "#": zyaml.CommentTokenizer,
            # "{": zyaml.FlowTokenizer,
            # "[": zyaml.FlowTokenizer,
            # '"': zyaml.DoubleQuoteTokenizer,
            # "'": zyaml.SingleQuoteTokenizer,
        }
        line = column = 1
        prev = " "
        # pos = 0
        current = tokenizer = None

        for pos, upcoming in enumerate(settings.buffer):
            if current is None:
                current = upcoming
                continue

            if tokenizer is not None:
                result = tokenizer(line, column, pos, prev, current, upcoming)
                if result is not None:
                    tokenizer = None

            else:
                tokenizer = zyaml.get_tokenizer(tokenizer_map, settings, line, column, pos, prev, current, upcoming)

            if current == "\n":
                line += 1
                column = 1

            else:
                column += 1

            prev = current
            current = upcoming


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


for i in YmlImplementation.__subclasses__():
    IMPLEMENTATIONS.append(i())


if __name__ == "__main__":
    main()
