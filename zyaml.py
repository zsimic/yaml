import codecs
import collections
import re


NULL = ("null", "~")
FALSE = "false"
TRUE = "true"
RE_TYPED = re.compile(r"^(false|true|null|[-+]?[0-9]*\.?[0-9]+([eE][-+]?[0-9]+)?)$", re.IGNORECASE)
RE_LINE_SPLIT = re.compile(r"^((\s*[%#]).*|(\s*-|---|\.\.\.)(\s.*)?)$")
RE_FLOW_SEP = re.compile(r"""(\s*)(#.*|[!&*]\S+|[\[\]{}"',]|:(\s+|$))""")
RE_BLOCK_SEP = re.compile(r"""(\s*)(#.*|[!&*]\S+|[\[\]{}"'>|]|:(\s+|$))""")
RE_DOUBLE_QUOTE_END = re.compile(r'([^\\]")')
RE_SINGLE_QUOTE_END = re.compile(r"([^']'([^']|$))")

try:
    basestring  # noqa, remove once py2 is dead
except NameError:
    basestring = str


def default_marshal(value):
    if not isinstance(value, basestring):
        return value

    text = value.strip()
    if not text:
        return value

    m = RE_TYPED.match(text)
    if not m:
        return text

    text = text.lower()
    if text in NULL:
        return None

    if text == FALSE:
        return False

    if text == TRUE:
        return True

    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def decode(value):
    """Python 2/3 friendly decoding of output"""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def decommented(text):
    if not text:
        return text
    if text.startswith("#"):
        return ""
    try:
        i = text.index(" #")
        return text[:i].rstrip()
    except ValueError:
        return text


def get_indent(text):
    count = 0
    for c in text:
        if c != " ":
            return count
        count += 1
    return count


class Token(object):
    """Scanned token, visitor pattern is used for parsing"""

    def __init__(self, line_number, indent, value=None):
        self.line_number = line_number
        self.indent = indent
        self.value = value

    def __repr__(self):
        name = self.__class__.__name__
        if self.indent is not None:
            name = "%s[%s,%s]" % (name, self.line_number, self.indent + 1)
        if self.value is None:
            return name
        return "%s %s" % (name, self.represented_value())

    def represented_value(self):
        return str(self.value)

    def consume_token(self, root):
        """
        :param RootNode root: Process this token on given 'root' node
        """


class StreamStartToken(Token):
    pass


class StreamEndToken(Token):
    def consume_token(self, root):
        root.pop_doc()


class DocumentStartToken(Token):
    def consume_token(self, root):
        root.pop_doc()


class DocumentEndToken(Token):
    def consume_token(self, root):
        root.pop_doc()


class FlowMappingStartToken(Token):
    def consume_token(self, root):
        root.push(MapNode(root, None))


class FlowSequenceStartToken(Token):
    def consume_token(self, root):
        root.push(ListNode(root, None))


class FlowEndToken(Token):
    def consume_token(self, root):
        root.pop()


class FlowEntryToken(Token):
    def consume_token(self, root):
        root.auto_apply()


class BlockEntryToken(Token):
    def __init__(self, line_number, indent):
        super(BlockEntryToken, self).__init__(line_number, indent)

    def consume_token(self, root):
        root.ensure_node(self.indent, ListNode)


class CommentToken(Token):
    pass


class DirectiveToken(Token):
    def __init__(self, line_number, indent, text):
        text = decommented(text)
        if text.startswith("%YAML"):
            self.name = "%YAML"
            text = text[5:].strip()
        elif text.startswith("%TAG"):
            self.name = "%TAG"
            text = text[4:].strip()
        else:
            self.name, _, text = text.partition(" ")
        super(DirectiveToken, self).__init__(line_number, indent, text.strip())

    def represented_value(self):
        return "%s %s" % (self.name, self.value)


class AnchorToken(Token):
    def consume_token(self, root):
        root.set_anchor(self)


class AliasToken(Token):
    def consume_token(self, root):
        value = root.anchors.get(self.value)
        root.push_value(self.indent, value)


class TagToken(Token):
    def __init__(self, line_number, indent, text, marshaller):
        super(TagToken, self).__init__(line_number, indent, text)
        self.marshaller = marshaller

    def consume_token(self, root):
        if root.marshaller:
            raise ParseError("2 consecutive tags given")
        root.marshaller = self.marshaller
        root.tag_indent = self.indent


class EmptyLineToken(Token):
    def consume_token(self, root):
        pass


class KeyToken(Token):
    def consume_token(self, root):
        root.push_key(self.indent, self.value)


class ScalarToken(Token):
    def __init__(self, line_number, indent, text=None, style=None):
        super(ScalarToken, self).__init__(line_number, indent, text)
        self.style = style

    def set_raw_lines(self, lines):
        self.set_raw_text(" ".join(lines))

    def set_raw_text(self, text):
        if self.style == "'":
            text = text.replace("''", "'")
        elif self.style == '"':
            text = codecs.decode(text, "unicode_escape")
        self.value = text

    def represented_value(self):
        if self.style is None:
            return str(self.value)
        if self.style == '"':
            return '"%s"' % decode(codecs.encode(self.value, "unicode_escape"))
        if self.style == "'":
            return "'%s'" % self.value.replace("'", "''")
        return "%s %s" % (self.style, self.value)

    def consume_token(self, root):
        root.push_value(self.indent, self.value)


def get_min(v1, v2):
    if v1 is None:
        return v2
    if v2 is None:
        return v1
    if v1 < v2:
        return v1
    return v2


class ParseNode(object):
    def __init__(self, root, indent):
        """
        :param RootNode root:
        :param int|None indent:
        """
        self.root = root  # type: RootNode
        if root.marshaller is not None:
            self.indent = get_min(indent, root.tag_indent)
            self.marshaller = root.marshaller
            root.marshaller = None
            root.tag_indent = None
        else:
            self.indent = indent
            self.marshaller = None
        self.prev = None
        self.is_temp = False
        self.needs_apply = False
        self.last_value = None
        self.target = None
        self.anchor_token = None

    def __repr__(self):
        result = "%s%s%s" % (self.__class__.__name__[0], "" if self.indent is None else self.indent, "*" if self.is_temp else "")
        if self.prev:
            result = "%s / %s" % (result, self.prev)
        return result

    def marshalled(self, value):
        if self.marshaller is not None:
            value = self.marshaller.marshalled(value)
            self.marshaller = None
        return value

    def set_key(self, key):
        raise ParseError("Key not allowed here")

    def set_value(self, value):
        self.needs_apply = True
        if self.last_value is None:
            self.last_value = value
        elif value is not None:
            self.last_value = "%s %s" % (self.last_value, value)

    def auto_apply(self):
        if self.anchor_token:
            self.root.anchors[self.anchor_token.value] = self.last_value
            self.anchor_token = None
        if self.needs_apply:
            self.apply()

    def apply(self):
        """Apply 'self.last_value' to 'self.target'"""
        self.needs_apply = False


class ListNode(ParseNode):
    def apply(self):
        if self.target is None:
            self.target = []
        self.target.append(self.last_value)
        self.last_value = None
        self.needs_apply = False


class MapNode(ParseNode):
    def __init__(self, root, indent):
        super(MapNode, self).__init__(root, indent)
        self.last_key = None

    def set_key(self, key):
        if self.last_key is not None:
            raise ParseError("Internal error, previous key '%s' was not consumed" % self.last_key)
        self.last_key = key
        self.needs_apply = True

    def apply(self):
        if self.target is None:
            self.target = {}
        self.target[self.last_key] = self.last_value
        self.last_key = None
        self.last_value = None
        self.needs_apply = False


class ScalarNode(ParseNode):
    def apply(self):
        self.target = self.last_value
        self.last_value = None
        self.needs_apply = False


class RootNode(object):
    def __init__(self):
        self.docs = []
        self.head = None  # type: ParseNode | None
        self.marshaller = None
        self.tag_indent = None
        self.doc_consumed = True
        self.anchors = {}

    def __repr__(self):
        return str(self.head or "/")

    def marshalled(self, value):
        if self.marshaller is not None:
            value = self.marshaller.marshalled(value)
            self.marshaller = None
        return value

    def set_anchor(self, token):
        self.head.anchor_token = token

    def auto_apply(self):
        if self.head:
            self.head.auto_apply()

    def needs_new_node(self, indent, node_type):
        if self.head is None or self.head.__class__ is not node_type:
            return True
        if indent is None:
            return self.head.indent is not None
        if self.head.indent is None:
            return False
        return indent > self.head.indent

    def needs_pop(self, indent):
        if indent is None or self.head is None or self.head.indent is None:
            return False
        return self.head.indent > indent

    def ensure_node(self, indent, node_type):
        while self.needs_pop(indent):
            self.pop()
        if self.needs_new_node(indent, node_type):
            if node_type is ListNode and self.head is not None and self.head.indent is not None and indent is not None:
                if indent < self.head.indent:
                    raise ParseError("Line should be indented at least %s chars" % self.head.indent)
            self.push(node_type(self, indent))
        self.auto_apply()

    def push_key(self, indent, key):
        self.ensure_node(indent, MapNode)
        self.head.set_key(key)

    def push_value(self, indent, value):
        value = self.marshalled(value)
        if self.head is None:
            self.push(ScalarNode(self, indent))
        self.head.set_value(value)
        if self.head.is_temp:
            self.pop()

    def push(self, node):
        """
        :param ParseNode node:
        """
        if self.head:
            if self.head.indent is None:
                node.is_temp = node.indent is not None
            elif node.indent is not None:
                while node.indent < self.head.indent:
                    self.pop()
        else:
            self.doc_consumed = False
        node.prev = self.head
        self.head = node

    def pop(self):
        popped = self.head
        self.head = popped.prev
        if popped:
            popped.auto_apply()
            value = popped.marshalled(popped.target)
            if self.head:
                self.head.set_value(value)
                self.head.auto_apply()
            else:
                self.set_value(value)
        else:
            raise ParseError("check")

    def set_value(self, value):
        self.doc_consumed = True
        value = self.marshalled(value)
        self.docs.append(value)

    def pop_doc(self):
        if self.head:
            while self.head:
                self.pop()
        elif not self.doc_consumed:
            self.set_value("")

    def deserialized(self, tokens):
        for token in tokens:
            token.consume_token(self)
        return simplified(self.docs)


class ParseError(Exception):
    def __init__(self, message, line_number=None, indent=None):
        self.message = message
        self.line_number = line_number
        self.indent = indent

    def __str__(self):
        if self.indent is None:
            return self.message
        return "%s, line %s column %s" % (self.message, self.line_number, self.indent + 1)

    def complete(self, line_number=None, indent=None):
        if self.line_number is None:
            self.line_number = line_number
        if self.indent is None:
            self.indent = indent


class Marshaller(object):
    def __init__(self, prefix=None, name=None):
        """
        :param str prefix: Tag prefix to which this marshaller belongs to
        :param str name: Tag name
        """
        self._prefix = prefix
        self._name = name

    def __repr__(self):
        return self.full_name()

    def full_name(self):
        return "!%s!%s" % (self.prefix(), self.name())

    def prefix(self):
        return getattr(self, "_prefix", "") or ""

    def name(self):
        if hasattr(self, "_name"):
            return self._name
        cls = self
        if not isinstance(cls, type):
            cls = self.__class__
        return cls.__name__.replace("Marshaller", "").lower()

    def marshalled(self, value):
        return value


class MapMarshaller(Marshaller):
    def marshalled(self, value):
        if isinstance(value, dict):
            return value
        if isinstance(value, list):
            if all(isinstance(x, dict) for x in value):
                result = {}
                for x in value:
                    result.update(x)
                return result
        raise ParseError("not a map")


class SeqMarshaller(Marshaller):
    def marshalled(self, value):
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            result = []
            for k, v in value.items():
                result.append(k)
                result.append(v)
            return result
        raise ParseError("not a list or map")


class SetMarshaller(Marshaller):
    def marshalled(self, value):
        if isinstance(value, dict):
            return set(value.keys())
        raise ParseError("not a map, !!set applies to maps")


class ScalarMarshaller(Marshaller):
    def marshalled(self, value):
        if isinstance(value, list):
            raise ParseError("scalar needed, got list instead")
        if isinstance(value, dict):
            raise ParseError("scalar needed, got map instead")
        return self._marshalled(value)

    def _marshalled(self, value):
        return value


class StrMarshaller(ScalarMarshaller):
    def _marshalled(self, value):
        return str(value)


class IntMarshaller(ScalarMarshaller):
    def _marshalled(self, value):
        return int(value)


class NullMarshaller(ScalarMarshaller):
    def _marshalled(self, value):
        return None


class BoolMarshaller(ScalarMarshaller):
    def _marshalled(self, value):
        text = str(value).lower()
        if text in (FALSE, "n", "no", "off"):
            return False
        if text in (TRUE, "y", "yes", "on"):
            return True
        raise ParseError("'%s' is not a boolean" % value)


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


class Scanner(object):
    def __init__(self, buffer):
        if hasattr(buffer, "read"):
            buffer = buffer.read()
        self.gen = enumerate(buffer.splitlines(), start=1)
        self.line_number = None
        self.line_pos = 0
        self.line_size = 0
        self.line_text = None
        self.pending = collections.deque()
        self.flow_ender = collections.deque()
        marshallers = get_descendants(Marshaller, adjust=lambda x: x.replace("Marshaller", "").lower())
        self.marshallers = {"": dict((name, m("", name)) for name, m in marshallers.items())}
        self.leaders = {
            "%": self.consume_directive,
            "-": self.consume_block_entry,
            "---": self.consume_doc_start,
            "...": self.consume_doc_end,
        }
        self.tokenizer_map = {
            "#": self.consume_comment,
            ":": self.consume_colon,
            "!": self.consume_tag,
            "&": self.consume_anchor,
            "*": self.consume_alias,
            ">": self.consume_literal,
            "|": self.consume_literal,
            "{": self.consume_flow_map_start,
            "}": self.consume_flow_map_end,
            "[": self.consume_flow_list_start,
            "]": self.consume_flow_list_end,
            ",": self.consume_comma,
            '"': self.consume_double_quote,
            "'": self.consume_single_quote,
        }

    def __repr__(self):
        return "%s [%s]: %s" % (self.line_number, self.line_pos, self.line_text)

    def get_marshaller(self, text):
        if text.startswith("!"):
            text = text[1:]
        prefix, _, name = text.partition("!")
        category = self.marshallers.get(prefix)
        if category:
            return category.get(name)

    def consume_directive(self):
        if self.line_text[0] == " ":
            raise ParseError("Directive must not be indented")
        self.line_pos = self.line_size
        return DirectiveToken(self.line_number, 0, self.line_text)

    def consume_block_entry(self):
        indent = get_indent(self.line_text)
        self.line_pos = indent + 2
        return BlockEntryToken(self.line_number, indent)

    def consume_doc_start(self):
        self.line_pos = 4
        return DocumentStartToken(self.line_number, 0)

    def consume_doc_end(self):
        self.line_pos = 4
        return DocumentEndToken(self.line_number, 0)

    def next_line(self, keep_comments=False):
        while True:
            self.line_number, self.line_text = next(self.gen)
            m = RE_LINE_SPLIT.match(self.line_text)
            if m is None:
                self.line_pos = 0
                self.line_size = len(self.line_text)
                return None
            self.line_pos, self.line_size = m.span(1)
            leader = m.group(2) or m.group(3)
            if leader is None:
                return None
            leader = leader.strip()
            if leader != "#":
                return self.leaders.get(leader)()
            if keep_comments:
                return None

    def consume_comment(self, start, end):
        pass

    def consume_colon(self, start, _):
        return KeyToken(self.line_number, start)

    def consume_tag(self, start, end):
        text = self.line_text[start:end]
        return TagToken(self.line_number, start, text, self.get_marshaller(text))

    def consume_anchor(self, start, end):
        return AnchorToken(self.line_number, start, self.line_text[start:end])

    def consume_alias(self, start, end):
        return AliasToken(self.line_number, start, self.line_text[start:end])

    def _get_literal_styled_token(self, start, style):
        original = style
        if len(style) > 3:
            raise ParseError("Invalid literal style '%s', should be less than 3 chars" % style, self.line_number, start)
        keep = None
        if "-" in style:
            style = style.replace("-", "", 1)
            keep = False
        if "+" in style:
            if keep is not None:
                raise ParseError("Ambiguous literal style '%s'" % original, self.line_number, start)
            keep = True
            style = style.replace("+", "", 1)
        indent = None
        if len(style) == 2:
            indent = style[1]
            style = style[0]
            if not indent.isdigit():
                raise ParseError("Invalid literal style '%s'" % original, self.line_number, start)
            indent = int(indent)
            if indent < 1:
                raise ParseError("Indent must be between 1 and 9", self.line_number, start)
        if style == ">":
            folded = True
        elif style == "|":
            folded = False
        else:
            raise ParseError("Internal error, invalid style '%s'" % original, self.line_number, start)
        return folded, keep, indent, ScalarToken(self.line_number, indent, style=original)

    def consume_literal(self, start, _):
        folded, keep, indent, token = self._get_literal_styled_token(start, decommented(self.line_text[start:]))
        lines = []
        while True:
            self.next_line(keep_comments=True)
            if not self.line_text:
                lines.append(self.line_text)
                continue
            i = get_indent(self.line_text)
            if indent is None:
                indent = i if i != 0 else 1
            if i < indent:
                text = "\n".join(lines)
                if keep is None:
                    token.value = "%s\n" % text.rstrip()
                elif keep is False:
                    token.value = text.rstrip()
                else:
                    token.value = "%s\n" % text
                return token
            value = self.line_text[indent:]
            if folded and lines and not value.startswith(" ") and not lines[-1].startswith(" "):
                if lines[-1]:
                    lines[-1] = "%s %s" % (lines[-1], value)
                else:
                    lines[-1] = value
            else:
                lines.append(value)

    def consume_flow_map_start(self, start, _):
        self.flow_ender.append("}")
        return FlowMappingStartToken(self.line_number, start)

    def pop_flow_ender(self, expected):
        try:
            popped = self.flow_ender.pop()
            if popped != expected:
                raise ParseError("Expecting '%s', but found '%s'" % (expected, popped))
        except IndexError:
            raise ParseError("'%s' without corresponding opener" % expected)

    def consume_flow_map_end(self, start, _):
        self.pop_flow_ender("}")
        return FlowEndToken(self.line_number, start)

    def consume_flow_list_start(self, start, _):
        self.flow_ender.append("]")
        return FlowSequenceStartToken(self.line_number, start)

    def consume_flow_list_end(self, start, _):
        self.pop_flow_ender("]")
        return FlowEndToken(self.line_number, start)

    def consume_comma(self, start, _):
        return FlowEntryToken(self.line_number, start)

    def _consume_multiline(self, start, style, regex):
        token = ScalarToken(self.line_number, start, style=style)
        try:
            start = start + 1
            lines = None
            m = None
            while m is None:
                m = regex.search(self.line_text, start)
                if m is not None:
                    end = self.line_pos = m.span(1)[1]
                    text = self.line_text[start:end]
                    text = text[:-1] if text.endswith(style) else text[:-2]
                    if lines is None:
                        token.set_raw_text(text)
                    else:
                        lines.append(text)
                        token.set_raw_lines(lines)
                    return token
                if lines is None:
                    lines = [self.line_text[start:]]
                    start = 0
                else:
                    lines.append(self.line_text)
                self.next_line(keep_comments=True)
        except StopIteration:
            raise ParseError("Unexpected end, runaway string at line %s?" % token.line_number)

    def consume_double_quote(self, start, _):
        return self._consume_multiline(start, '"', RE_DOUBLE_QUOTE_END)

    def consume_single_quote(self, start, _):
        return self._consume_multiline(start, "'", RE_SINGLE_QUOTE_END)

    def tokenized(self, start, end):
        if start == end:
            return EmptyLineToken(self.line_number, start)
        tokenizer = self.tokenizer_map.get(self.line_text[start])
        if tokenizer is not None:
            return tokenizer(start, end)
        return ScalarToken(self.line_number, start, text=self.line_text[start:end])

    def next_token(self, regex):
        if self.pending:
            start, end = self.pending.pop()
            self.line_pos = end
            return self.tokenized(start, end)
        if self.line_pos >= self.line_size:
            token = self.next_line()
            if token is not None:
                return token
        start = self.line_pos
        end = self.line_size
        if start < end:
            m = regex.search(self.line_text, start)
            if m:
                prev_start = start
                start, end = m.span(2)
                if m.span(1)[0] > prev_start:
                    self.pending.append((start, end))
                    self.line_pos = start
                    return self.tokenized(prev_start, start)
            else:
                end = self.line_size
            self.line_pos = end
        return self.tokenized(start, end)

    def __iter__(self):
        token = None
        try:
            yield StreamStartToken(1, 0)
            while True:
                token = self.next_token(RE_FLOW_SEP if self.flow_ender else RE_BLOCK_SEP)
                if token is not None:
                    yield token
        except StopIteration:
            yield StreamEndToken(self.line_number, 0)
        except ParseError as error:
            if token is not None:
                error.complete(token.line_number, token.indent)
            error.complete(self.line_number, self.line_pos)
            raise


def load(stream):
    """
    :param str|file stream: Stream or contents to load
    """
    if hasattr(stream, "read"):
        stream = stream.read()
    return load_string(stream)


def load_string(contents):
    """
    :param str contents: Yaml to deserialize
    """
    scanner = Scanner(contents)
    return RootNode().deserialized(scanner)


def load_path(path):
    """
    :param str path: Path to file to deserialize
    """
    with open(path) as fh:
        return load_string(fh.read())


def simplified(docs):
    if isinstance(docs, list) and len(docs) == 1:
        return docs[0]
    return docs
