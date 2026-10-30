from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
from html.entities import html5
from html.parser import HTMLParser
import html.parser as html_parser
import json
import math
import re
from urllib.parse import urljoin, urlsplit

MAX_HTML = 4 * 1024 * 1024
MAX_RULES = 32 * 1024
MAX_NODES = 20_000
MAX_DEPTH = 64
MAX_ATTRIBUTES = 32
MAX_ATTRIBUTE_NAME_BYTES = 64
MAX_ATTRIBUTE_BYTES = 64 * 1024
UTF8_BOM = b"\xef\xbb\xbf"
CONTENT_MODEL = "static_html_no_js"
MAX_ITEMS = 200
MAX_FIELDS = 16
MAX_VALUE = 2048
MAX_WORK = 500_000
MAX_OUTPUT = 3 * 1024 * 1024
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
HIDDEN = {"script", "style", "template", "noscript"}
TEXT_ONLY = {"script", "style", "textarea", "title", "noscript", "xmp", "iframe", "noembed", "noframes"}
OPTIONAL_END = {"p", "li", "dt", "dd", "td", "th", "tr", "thead", "tbody", "tfoot", "option", "optgroup", "colgroup"}
P_BLOCKS = set("address article aside blockquote details dialog div dl fieldset figcaption figure footer form h1 h2 h3 h4 h5 h6 header hgroup hr main menu nav ol p pre search section table ul li dt dd tr td th".split())
IMPLIED_BOUNDARIES = {"li": ({"li"}, {"ul", "ol"}), "dt": ({"dt", "dd"}, {"dl"}),
                      "dd": ({"dt", "dd"}, {"dl"}), "td": ({"td", "th"}, {"tr", "table"}),
                      "th": ({"td", "th"}, {"tr", "table"}), "tr": ({"tr"}, {"table", "tbody", "thead", "tfoot"}),
                      "option": ({"option"}, {"select", "datalist"}), "optgroup": ({"option", "optgroup"}, {"select"})}
LINK_SCOPES = {"td", "th", "caption", "template", "object", "applet", "marquee"}
FOREIGN_BREAKOUT = set("b big blockquote body br center code dd div dl dt em embed h1 h2 h3 h4 h5 h6 head hr i img li listing menu meta nobr ol p pre ruby s small span strong strike sub sup table tt u ul var".split())
INLINE = set("a abbr acronym b bdi bdo big cite code data del dfn em font i img ins kbd label mark nobr q rb rp rt ruby s samp small span strike strong sub sup time tt u var wbr".split())
ASCII_SPACE = r"[\t\n\f\r ]+"
IDENT = r"[A-Za-z_][A-Za-z0-9_-]*"


def _needs_attribute_guard():
    class Probe(HTMLParser):
        value = None

        def handle_starttag(self, tag, attrs):
            self.value = dict(attrs).get("href")

    parser = Probe()
    parser.feed('<a href="?a&notx=1&not=2">')
    parser.close()
    return parser.value != "?a&notx=1&not=2"


_ATTRIBUTE_GUARD = _needs_attribute_guard()
_AMBIGUOUS_ATTRIBUTE_REF = re.compile(r"&(?:" + "|".join(re.escape(name) for name in
    sorted((name for name in html5 if not name.endswith(";")), key=len, reverse=True)) + r")(?=[=A-Za-z0-9])")
_NAMED_REF = re.compile(r"&[A-Za-z][A-Za-z0-9]*;?")


def _ambiguous_reference(value):
    return any(not (ref.group().endswith(";") and ref.group()[1:] in html5)
               and _AMBIGUOUS_ATTRIBUTE_REF.match(value, ref.start()) for ref in _NAMED_REF.finditer(value))


def _ambiguous_attributes(raw):
    if not _ATTRIBUTE_GUARD or not raw or "&" not in raw:
        return frozenset()
    # Use this interpreter's own attribute token boundaries. These private
    # patterns are optional: an unfamiliar parser fails closed on risky tags.
    tag_pattern = getattr(html_parser, "tagfind_tolerant", None)
    attr_pattern = getattr(html_parser, "attrfind_tolerant", None)
    start = tag_pattern.match(raw, 1) if tag_pattern else None
    if not start or not attr_pattern:
        if _ambiguous_reference(raw):
            raise ContractError("html", "This Python HTML parser cannot safely read ambiguous attribute references. Update Python or encode attribute ampersands as &amp;.")
        return frozenset()
    risky, position = set(), start.end()
    while match := attr_pattern.match(raw, position):
        if _ambiguous_reference(match.group(3) or ""):
            risky.add(match.group(1).lower())
        position = match.end()
    return frozenset(risky)


class ContractError(ValueError):
    def __init__(self, code: str, message: str, issues: list[dict] | None = None):
        super().__init__(message)
        self.code, self.message, self.issues = code, message, issues or []

    def as_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": self.message, "issues": self.issues}


def html_limit(kind: str, message: str, *, limit: int, **numbers: int) -> ContractError:
    # Limit errors carry only fixed labels and numbers, never source text or names.
    return ContractError("html_limit", message, [{"code": kind, "limit": limit, **numbers}])


def html_bytes_error(size: int | None = None) -> ContractError:
    if size is None:
        return html_limit("html_bytes", f"HTML is larger than the {MAX_HTML:,}-byte UTF-8 limit.", limit=MAX_HTML)
    return html_limit("html_bytes", f"HTML is {size:,} UTF-8 bytes; the limit is {MAX_HTML:,} bytes.", limit=MAX_HTML, actual=size)


def decode_html(data: bytes) -> str:
    """Strictly decode UTF-8 HTML bytes after removing one leading byte-order mark.

    The size limit applies to the bytes after the mark. Invalid bytes are
    rejected, never replaced.
    """
    offset = len(UTF8_BOM) if data.startswith(UTF8_BOM) else 0
    if len(data) - offset > MAX_HTML:
        raise html_bytes_error(len(data) - offset)
    try:
        return data[offset:].decode("utf-8")
    except UnicodeDecodeError as exc:
        position = offset + exc.start
        raise ContractError("html_encoding", f"HTML is not valid UTF-8: invalid byte sequence at byte offset {position:,}. Save or export the page as UTF-8.",
                            [{"code": "invalid_utf8", "offset": position}]) from None


def encode_html(html) -> bytes:
    if not isinstance(html, str):
        raise ContractError("html", "HTML must be provided as text.")
    try:
        data = html.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ContractError("html_encoding", f"HTML text contains an unpaired surrogate at character {exc.start:,}, so it cannot be encoded as UTF-8.",
                            [{"code": "unpaired_surrogate", "offset": exc.start}]) from None
    if len(data) > MAX_HTML:
        raise html_bytes_error(len(data))
    return data


def json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("json", "Duplicate JSON keys are ambiguous.")
        result[key] = value
    return result


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str]
    parent: Node | None = None
    children: list[Node | str] = field(default_factory=list)
    ambiguous_attrs: frozenset[str] = frozenset()
    classes: frozenset[str] = field(init=False)
    namespace: str = "html"
    elements: list[Node] = field(default_factory=list)

    def __post_init__(self):
        self.classes = frozenset(filter(None, re.split(ASCII_SPACE, self.attrs.get("class", ""))))


class Document(HTMLParser):
    def __init__(self):
        try:
            super().__init__(convert_charrefs=True, scripting=True)
        except TypeError:
            # Older Python patch levels lack scripting/RCDATA support. Refuse
            # nested markup callbacks there rather than fabricate child fields.
            super().__init__(convert_charrefs=True)
        self.root = Node("document", {})
        self.stack = [self.root]
        self.nodes = 0
        self.scripts = 0

    def _namespace(self, tag):
        parent = self.stack[-1]
        namespace = parent.namespace
        if namespace == "svg" and parent.tag in {"foreignobject", "desc", "title"}:
            namespace = "html"
        elif namespace == "math":
            if (parent.tag in {"mi", "mo", "mn", "ms", "mtext"} and tag not in {"mglyph", "malignmark"}
                    or parent.tag == "annotation-xml" and parent.attrs.get("encoding", "").lower() in {"text/html", "application/xhtml+xml"}):
                namespace = "html"
            elif parent.tag == "annotation-xml" and tag == "svg":
                return "svg"
        if namespace == "html" and tag in {"svg", "math"}:
            return tag
        return namespace

    def handle_starttag(self, tag, attrs):
        self._guard_text_only()
        ambiguous = _ambiguous_attributes(self.get_starttag_text())
        namespace = self._namespace(tag)
        if namespace != "html" and (tag in FOREIGN_BREAKOUT or tag == "font" and any(key in {"color", "face", "size"} for key, _ in attrs)):
            raise ContractError("html", "HTML elements inside foreign markup need explicit supported integration points.")
        html_stack = []
        if namespace == "html":
            for node in reversed(self.stack):
                if node.namespace != "html":
                    break
                html_stack.append(node)
        if namespace == "html" and tag in P_BLOCKS and any(node.tag == "p" for node in html_stack):
            raise ContractError("html", "Write optional HTML closing tags explicitly before extracting fields.")
        if namespace == "html" and tag in IMPLIED_BOUNDARIES:
            previous, containers = IMPLIED_BOUNDARIES[tag]
            for node in html_stack:
                if node.tag in containers:
                    break
                if node.tag in previous:
                    raise ContractError("html", "Write optional HTML closing tags explicitly before extracting fields.")
        if namespace == "html" and tag == "a":
            for node in reversed(self.stack):
                if node.namespace != "html":
                    continue
                if node.tag in LINK_SCOPES:
                    break
                if node.tag == "a":
                    raise ContractError("html", "Close each link before starting another; browsers split nested links.")
        self.nodes += 1
        line = self.getpos()[0]
        if self.nodes > MAX_NODES:
            raise html_limit("html_elements", f"HTML has more than {MAX_NODES:,} elements (limit reached at line {line:,}).", limit=MAX_NODES, line=line)
        if len(self.stack) > MAX_DEPTH:
            raise html_limit("html_depth", f"HTML nesting at line {line:,} is deeper than {MAX_DEPTH} levels.", limit=MAX_DEPTH, actual=len(self.stack), line=line)
        if len(attrs) > MAX_ATTRIBUTES:
            raise html_limit("html_attributes", f"An HTML element at line {line:,} has {len(attrs)} attributes; the limit is {MAX_ATTRIBUTES}.", limit=MAX_ATTRIBUTES, actual=len(attrs), line=line)
        for key, value in attrs:
            name_size = len(key.encode("utf-8", "surrogatepass"))
            if name_size > MAX_ATTRIBUTE_NAME_BYTES:
                raise html_limit("html_attribute_name", f"An HTML attribute name at line {line:,} is {name_size:,} bytes; the limit is {MAX_ATTRIBUTE_NAME_BYTES} bytes.",
                                 limit=MAX_ATTRIBUTE_NAME_BYTES, actual=name_size, line=line)
            # Each UTF-8 character is at most four bytes, so short values skip encoding.
            if value and len(value) * 4 > MAX_ATTRIBUTE_BYTES:
                size = len(value.encode("utf-8", "surrogatepass"))
                if size > MAX_ATTRIBUTE_BYTES:
                    raise html_limit("html_attribute_bytes", f"An HTML attribute value at line {line:,} is {size:,} UTF-8 bytes; the limit is {MAX_ATTRIBUTE_BYTES:,} bytes.",
                                     limit=MAX_ATTRIBUTE_BYTES, actual=size, line=line)
        if namespace == "html" and tag == "script":
            self.scripts += 1
        if len({key for key, _ in attrs}) != len(attrs):
            raise ContractError("html", "Duplicate HTML attributes are ambiguous.")
        node = Node(tag, dict((key, value or "") for key, value in attrs), self.stack[-1], ambiguous_attrs=ambiguous, namespace=namespace)
        self.stack[-1].children.append(node)
        self.stack[-1].elements.append(node)
        if namespace != "html" or tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        namespace = self._namespace(tag)
        if namespace == "html" and tag not in VOID:
            if tag in TEXT_ONLY:
                raise ContractError("html", "Text-only HTML elements need explicit start and end tags.")
            raise ContractError("html", "Non-void HTML elements need explicit start and end tags.")
        self.handle_starttag(tag, attrs)
        if namespace != "html" or tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if self.stack[-1].tag != tag:
            self._guard_text_only()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                if any(node.tag in OPTIONAL_END for node in self.stack[index + 1:]):
                    raise ContractError("html", "Write optional HTML closing tags explicitly before extracting fields.")
                del self.stack[index:]
                break

    def _guard_text_only(self):
        if self.stack[-1].tag in TEXT_ONLY:
            raise ContractError("html", "This Python HTML parser cannot safely read markup inside a text-only element. Update Python or use a simpler capture.")

    def handle_comment(self, data):
        self._guard_text_only()

    def handle_decl(self, decl):
        self._guard_text_only()

    def handle_pi(self, data):
        self._guard_text_only()

    def unknown_decl(self, data):
        self._guard_text_only()

    def close(self):
        super().close()
        if any(node.tag in OPTIONAL_END for node in self.stack):
            raise ContractError("html", "Write optional HTML closing tags explicitly before extracting fields.")

    def handle_data(self, data):
        if data and not any(node.tag in HIDDEN for node in self.stack):
            self.stack[-1].children.append(data)


@dataclass(frozen=True)
class Simple:
    tag: str = "*"
    identifier: str | None = None
    classes: tuple[str, ...] = ()
    attrs: tuple[tuple[str, str | None], ...] = ()

    def matches(self, node: Node) -> bool:
        if self.tag != "*" and self.tag != node.tag:
            return False
        if node.ambiguous_attrs:
            used = ({"id"} if self.identifier is not None else set()) | ({"class"} if self.classes else set())
            used.update(key for key, value in self.attrs if value is not None)
            if node.ambiguous_attrs.intersection(used):
                raise ContractError("html", "A selector uses an attribute this Python HTML parser may change. Update Python or encode attribute ampersands as &amp;.")
        return ((self.tag == "*" or self.tag == node.tag)
                and (self.identifier is None or node.attrs.get("id") == self.identifier)
                and all(name in node.classes for name in self.classes)
                and all(key in node.attrs and (value is None or node.attrs[key] == value) for key, value in self.attrs))


def simple_selector(token: str) -> Simple:
    match = re.match(r"[A-Za-z][A-Za-z0-9-]*|\*", token)
    tag, position = (match.group().lower(), match.end()) if match else ("*", 0)
    identifier, classes, attrs = None, [], []
    while position < len(token):
        rest = token[position:]
        if rest[0] in ".#":
            found = re.match(r"[.#](" + IDENT + ")", rest)
            if not found:
                raise ContractError("selector", "Unsupported selector syntax.")
            if rest[0] == "#":
                if identifier is not None:
                    raise ContractError("selector", "A selector may contain only one ID per step.")
                identifier = found.group(1)
            else:
                classes.append(found.group(1))
        else:
            found = re.match(r"\[([A-Za-z_][A-Za-z0-9_-]*)(?:=(?:\"([^\"]{0,128})\"|'([^']{0,128})'|([A-Za-z0-9_./:-]{1,128})))?\]", rest)
            if not found:
                raise ContractError("selector", "Use tags, classes, IDs, attributes, spaces or > only.")
            attrs.append((found.group(1).lower(), next((v for v in found.groups()[1:] if v is not None), None)))
        position += found.end()
        if len(classes) + len(attrs) > 8:
            raise ContractError("selector", "A selector step is too complex.")
    return Simple(tag, identifier, tuple(classes), tuple(attrs))


def compile_selector(selector: str) -> tuple[tuple[Simple, str], ...]:
    if not isinstance(selector, str) or not 1 <= len(selector) <= 256:
        raise ContractError("selector", "Selectors must contain 1 to 256 characters.")
    if selector == ":scope":
        return ()
    tokens, buffer, relation, quote, brackets = [], "", "descendant", None, 0
    for char in selector.strip(" \t\n\r\f"):
        if quote:
            buffer += char
            if char == quote:
                quote = None
        elif char in "\"'" and brackets:
            quote = char
            buffer += char
        elif char == "[":
            brackets += 1
            buffer += char
        elif char == "]":
            brackets -= 1
            buffer += char
        elif brackets == 0 and (char in " \t\n\r\f" or char == ">"):
            if buffer:
                tokens.append((simple_selector(buffer), relation))
                buffer, relation = "", "descendant"
            if char == ">":
                if not tokens or relation == "child":
                    raise ContractError("selector", "Incomplete child selector.")
                relation = "child"
        else:
            buffer += char
        if brackets not in (0, 1):
            raise ContractError("selector", "Malformed attribute selector.")
    if buffer:
        tokens.append((simple_selector(buffer), relation))
    elif relation == "child":
        raise ContractError("selector", "Incomplete child selector.")
    if quote or brackets or not tokens or len(tokens) > 8:
        raise ContractError("selector", "Selector is incomplete or exceeds eight steps.")
    return tuple(tokens)


def work(budget, cost=1):
    budget[0] -= cost
    if budget[0] < 0:
        raise ContractError("work_limit", "Selector processing exceeded the work limit. Narrow the item selector or reduce the HTML.")


def query(scope: Node, selector: str, budget=None) -> list[Node]:
    return _query(scope, compile_selector(selector), budget)


def _query(scope: Node, steps: tuple[tuple[Simple, str], ...], budget=None) -> list[Node]:
    budget = budget if budget is not None else [MAX_WORK]
    if not steps:
        return [scope]
    output, pending, memo = [], list(reversed(scope.elements)), {}
    single = steps[0][0] if len(steps) == 1 else None

    def matches(current, index):
        if current is None or current is scope.parent:
            return False
        key = (id(current), index)
        if key in memo:
            return memo[key]
        work(budget)
        answer = False
        if steps[index][0].matches(current):
            if index == 0:
                answer = True
            elif steps[index][1] == "child":
                answer = matches(current.parent, index - 1)
            else:
                parent = current.parent
                while parent is not None and parent is not scope.parent:
                    work(budget)
                    if matches(parent, index - 1):
                        answer = True
                        break
                    parent = parent.parent
        memo[key] = answer
        return answer

    while pending:
        work(budget)
        node = pending.pop()
        if node.tag in HIDDEN:
            continue
        pending.extend(reversed(node.elements))
        if single is not None:
            # One step needs no ancestor recursion or per-node memo entry. Keep
            # the same work charge as the general matcher.
            work(budget)
            matched = single.matches(node)
        else:
            matched = matches(node, len(steps) - 1)
        if matched:
            output.append(node)
    return output


def node_text(node: Node, budget=None) -> str:
    budget = budget if budget is not None else [MAX_WORK]
    pieces, pending = [], list(reversed(node.children))
    while pending:
        work(budget)
        current = pending.pop()
        if current is None:
            pieces.append(" ")
        elif isinstance(current, str):
            work(budget, len(current) // 16)
            pieces.append(current)
        elif current.tag not in HIDDEN:
            if current.tag not in INLINE:
                pieces.append(" ")
                pending.append(None)
            pending.extend(reversed(current.children))
    return " ".join("".join(pieces).split())


def load_rules(value: dict | str) -> dict:
    try:
        if isinstance(value, str):
            size = len(value.encode())
            if size > MAX_RULES:
                raise ContractError("rules_limit", f"Rules are {size:,} UTF-8 bytes; the limit is {MAX_RULES:,} bytes.",
                                    [{"code": "rules_bytes", "limit": MAX_RULES, "actual": size}])
            value = json.loads(value, object_pairs_hook=json_object)
        if not isinstance(value, dict) or set(value) - {"item_selector", "min_items", "max_items", "expected_items", "fields", "base_url"}:
            raise ContractError("rules", "Rules must be an object with documented keys only.")
        if "base_url" in value:
            try:
                base = urlsplit(value["base_url"])
                valid_base = (len(value["base_url"]) <= 2048 and base.scheme in {"http", "https"} and bool(base.hostname)
                              and base.username is None and base.password is None and not base.query and not base.fragment
                              and (base.port is None or 1 <= base.port <= 65535)
                              and not any(char.isspace() for char in value["base_url"]) and "\\" not in value["base_url"])
            except (ValueError, TypeError, AttributeError):
                valid_base = False
            if not valid_base:
                raise ContractError("rules", "base_url must be an HTTP(S) resolution base without credentials, query or fragment.")
        compile_selector(value.get("item_selector"))
        for key, default, minimum in (("min_items", 1, 0), ("max_items", MAX_ITEMS, 1)):
            number = value.get(key, default)
            if type(number) is not int or not minimum <= number <= MAX_ITEMS:
                raise ContractError("rules", "Item limits must be integers from 0 to 200, with a positive maximum.")
        if value.get("min_items", 1) > value.get("max_items", MAX_ITEMS):
            raise ContractError("rules", "Minimum item count exceeds the maximum.")
        if "expected_items" in value and (type(value["expected_items"]) is not int or not 0 <= value["expected_items"] <= MAX_ITEMS):
            raise ContractError("rules", "Expected item count must be an integer from 0 to 200.")
        if "expected_items" in value and not value.get("min_items", 1) <= value["expected_items"] <= value.get("max_items", MAX_ITEMS):
            raise ContractError("rules", "Expected item count must be within min_items and max_items.")
        fields = value.get("fields")
        if not isinstance(fields, list) or not 1 <= len(fields) <= MAX_FIELDS:
            raise ContractError("rules", "Define 1 to 16 fields.")
        names = set()
        for item in fields:
            if not isinstance(item, dict) or set(item) - {"name", "selector", "attribute", "type", "required"}:
                raise ContractError("rules", "Each field must use documented keys only.")
            name = item.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,31}", name) or name in names:
                raise ContractError("rules", "Field names must be unique identifiers, up to 32 characters.")
            names.add(name)
            compile_selector(item.get("selector"))
            if item.get("type", "text") not in {"text", "integer", "number", "url"} or type(item.get("required", True)) is not bool:
                raise ContractError("rules", "Unsupported field type or required flag.")
            attribute = item.get("attribute")
            if attribute is not None and (not isinstance(attribute, str) or not re.fullmatch(r"(?:href|src|title|alt|value|data-[a-z0-9_-]{1,40})", attribute)):
                raise ContractError("rules", "Unsupported extraction attribute.")
        if len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > MAX_RULES:
            raise ContractError("rules_limit", f"Rules exceed the {MAX_RULES:,}-byte limit when serialized.", [{"code": "rules_bytes", "limit": MAX_RULES}])
        return value
    except ContractError:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractError("rules", "Rules must be valid, bounded UTF-8 JSON.") from exc


def convert(raw: str, kind: str, base_url: str | None):
    if len(raw) > MAX_VALUE:
        raise ValueError("field_too_long")
    if kind == "text":
        return raw
    if kind in {"number", "integer"}:
        if not re.fullmatch(r"[+-]?[0-9]{1,15}(?:\.[0-9]{1,8})?", raw):
            raise ValueError("type_changed")
        try:
            value = Decimal(raw)
            if abs(value) > 1_000_000_000_000 or (kind == "integer" and value != value.to_integral_value()):
                raise ValueError("type_changed")
            result = int(value) if kind == "integer" else float(value)
            if isinstance(result, float) and not math.isfinite(result):
                raise ValueError("type_changed")
            return result
        except InvalidOperation as exc:
            raise ValueError("type_changed") from exc
    result = urljoin(base_url or "https://example.invalid/", raw)
    parts = urlsplit(result)
    if (parts.scheme not in {"http", "https"} or not parts.netloc or parts.username is not None or parts.password is not None
            or any(char.isspace() for char in result) or "\\" in result or (parts.port is not None and not 1 <= parts.port <= 65535)):
        raise ValueError("unsafe_url")
    return result


def extract(html: str, rules: dict | str, *, source_kind="pasted", source_url: str | None = None, retrieved_at: str | None = None) -> dict:
    data = encode_html(html)
    rules = load_rules(rules)
    parser = Document()
    try:
        parser.feed(html)
        parser.close()
    except (RecursionError, ValueError, AssertionError) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("html", "Malformed or unsupported HTML markup.") from None
    budget = [MAX_WORK]
    # Compile within this extraction only; rule text is never retained in a global cache.
    selectors = {selector: compile_selector(selector) for selector in
                 (rules["item_selector"], *(spec["selector"] for spec in rules["fields"]))}
    rows = _query(parser.root, selectors[rules["item_selector"]], budget)
    if not rules.get("min_items", 1) <= len(rows) <= rules.get("max_items", MAX_ITEMS) or ("expected_items" in rules and len(rows) != rules["expected_items"]):
        raise ContractError("schema_drift", "Item count no longer matches the contract.", [{"code": "item_count", "actual": len(rows)}])
    items, issues, output_size = [], [], 0
    for index, row in enumerate(rows):
        record = {}
        for spec in rules["fields"]:
            matches = _query(row, selectors[spec["selector"]], budget)
            code = "ambiguous_field" if len(matches) > 1 else None
            if code:
                record[spec["name"]] = None
                issues.append({"item": index, "field": spec["name"], "code": code})
                continue
            raw = ""
            if len(matches) == 1:
                if spec.get("attribute") in matches[0].ambiguous_attrs:
                    raise ContractError("html", "A field uses an attribute this Python HTML parser may change. Update Python or encode attribute ampersands as &amp;.")
                raw = matches[0].attrs.get(spec["attribute"], "") if spec.get("attribute") else node_text(matches[0], budget)
                raw = raw.strip()
            if not matches or not raw:
                if spec.get("required", True):
                    code = "missing_required"
                record[spec["name"]] = None
            elif code is None:
                try:
                    record[spec["name"]] = convert(raw, spec.get("type", "text"), source_url or rules.get("base_url"))
                except ValueError as exc:
                    code = str(exc) if str(exc) in {"type_changed", "unsafe_url", "field_too_long"} else "invalid_field"
            if code:
                issues.append({"item": index, "field": spec["name"], "code": code})
            output_size += len(json.dumps({spec["name"]: record.get(spec["name"])}, ensure_ascii=False).encode("utf-8"))
            if output_size > MAX_OUTPUT - 64 * 1024:
                raise ContractError("output_limit", "Extraction output exceeds the 3 MiB limit. Use fewer items or fields.")
        items.append(record)
    if issues:
        raise ContractError("schema_drift", "Required fields, cardinality or types no longer match the contract.", issues)
    canonical = json.dumps(rules, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # source_hash covers the analyzed text as UTF-8: after any byte-order mark is
    # removed and before parsing. No other normalization is applied here.
    provenance = {"source_kind": source_kind, "source_hash": hashlib.sha256(data).hexdigest(),
                  "source_bytes": len(data), "content_model": CONTENT_MODEL,
                  "html_elements": parser.nodes, "script_elements": parser.scripts,
                  "rules_hash": hashlib.sha256(canonical.encode()).hexdigest(),
                  "retrieved_at": retrieved_at or datetime.now(timezone.utc).isoformat(),
                  "item_selector": rules["item_selector"],
                  "field_selectors": {item["name"]: item["selector"] for item in rules["fields"]}}
    if source_url:
        provenance["source_url"] = source_url
    if rules.get("base_url"):
        provenance["resolution_base"] = rules["base_url"]
    return {"ok": True, "items": items, "contract": {"item_count": len(items), "fields": {s["name"]: s.get("type", "text") for s in rules["fields"]}}, "provenance": provenance}
