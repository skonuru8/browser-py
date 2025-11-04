# browser_chrome_tabs_api.py
import socket, ssl, sys, urllib.parse, tkinter, tkinter.font

# Optional dependency for JavaScript execution. DukPy wraps the Duktape
# JavaScript engine. If it isn't available, interactive scripts will
# not run. Exercises in Chapter 9 rely on this module.
try:
    import dukpy  # type: ignore
except Exception:
    dukpy = None

# ================= Networking =================
class URL:
    def __init__(self, url):
        self.scheme, rest = url.split("://", 1)
        assert self.scheme in ["http", "https"]
        if "/" not in rest: rest += "/"
        self.host, path = rest.split("/", 1)
        self.path = "/" + path
        self.port = 80 if self.scheme == "http" else 443
        if ":" in self.host:
            self.host, p = self.host.split(":", 1)
            self.port = int(p)

    def request(self, payload=None):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        s.connect((self.host, self.port))
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            s = ctx.wrap_socket(s, server_hostname=self.host)

        method = "POST" if payload is not None else "GET"
        req = f"{method} {self.path} HTTP/1.0\r\nHost: {self.host}\r\n"
        if payload is not None:
            length = len(payload.encode("utf8"))
            req += "Content-Type: application/x-www-form-urlencoded\r\n"
            req += f"Content-Length: {length}\r\n"
        req += "\r\n"
        if payload is not None:
            req += payload

        s.send(req.encode("utf8"))
        resp = s.makefile("r", encoding="utf8", newline="\r\n")
        _ = resp.readline()
        headers = {}
        while True:
            line = resp.readline()
            if line == "\r\n": break
            k, v = line.split(":", 1)
            headers[k.casefold()] = v.strip()
        assert "transfer-encoding" not in headers
        assert "content-encoding" not in headers
        body = resp.read()
        s.close()
        return body

    def resolve(self, url):
        if "://" in url: return URL(url)
        if url.startswith("//"):
            return URL(self.scheme + ":" + url)
        if not url.startswith("/"):
            dir, _ = self.path.rsplit("/", 1)
            while url.startswith("../"):
                _, url = url.split("/", 1)
                if "/" in dir: dir, _ = dir.rsplit("/", 1)
            url = dir + "/" + url
        return URL(self.scheme + "://" + self.host + ":" + str(self.port) + url)

    def __str__(self):
        show_port = (
            (self.scheme == "http" and self.port != 80) or
            (self.scheme == "https" and self.port != 443)
        )
        port = f":{self.port}" if show_port else ""
        return f"{self.scheme}://{self.host}{port}{self.path}"

# ================= HTML nodes & parser =================
class Text:
    def __init__(self, text, parent):
        self.text = text
        self.children = []
        self.parent = parent
        self.style = {}
        self.is_focused = False
    def __repr__(self): return repr(self.text)

class Element:
    def __init__(self, tag, attributes, parent):
        self.tag = tag
        self.attributes = attributes
        self.children = []
        self.parent = parent
        self.style = {}
        self.is_focused = False
    def __repr__(self): return "<" + self.tag + ">"

def print_tree(node, indent=0):
    print("  " * indent + repr(node))
    for c in getattr(node, "children", []):
        print_tree(c, indent + 1)

def tree_to_list(tree, out):
    out.append(tree)
    for c in getattr(tree, "children", []):
        tree_to_list(c, out)
    return out

class HTMLParser:
    SELF_CLOSING_TAGS = [
        "area","base","br","col","embed","hr","img","input",
        "link","meta","param","source","track","wbr",
    ]
    HEAD_TAGS = [
        "base","basefont","bgsound","noscript",
        "link","meta","title","style","script",
    ]
    def __init__(self, body):
        self.body = body
        self.unfinished = []

    def parse(self):
        text = ""; in_tag = False
        for c in self.body:
            if c == "<":
                in_tag = True
                if text: self.add_text(text); text = ""
            elif c == ">":
                in_tag = False
                self.add_tag(text); text = ""
            else:
                text += c
        if not in_tag and text:
            self.add_text(text)
        return self.finish()

    def get_attributes(self, text):
        parts = text.split()
        if not parts: return "", {}
        tag = parts[0].casefold()
        attributes = {}
        for attrpair in parts[1:]:
            if "=" in attrpair:
                key, value = attrpair.split("=", 1)
                if len(value) > 2 and value[0] in ["'", '"']:
                    value = value[1:-1]
                attributes[key.casefold()] = value
            else:
                attributes[attrpair.casefold()] = ""
        return tag, attributes

    def implicit_tags(self, tag):
        while True:
            open_tags = [node.tag for node in self.unfinished]
            if open_tags == [] and tag != "html":
                self.add_tag("html")
            elif open_tags == ["html"] and tag not in ["head","body","/html"]:
                if tag in self.HEAD_TAGS: self.add_tag("head")
                else: self.add_tag("body")
            elif open_tags == ["html","head"] and \
                 tag not in ["/head"] + self.HEAD_TAGS:
                self.add_tag("/head")
            else:
                break

    def add_text(self, text):
        if text.isspace(): return
        self.implicit_tags(None)
        parent = self.unfinished[-1] if self.unfinished else None
        if parent is None:
            self.implicit_tags(None); parent = self.unfinished[-1]
        node = Text(text, parent)
        parent.children.append(node)

    def add_tag(self, tagtext):
        if tagtext.startswith("!"): return
        tag, attributes = self.get_attributes(tagtext)
        self.implicit_tags(tag)
        if tag.startswith("/"):
            if len(self.unfinished) == 1: return
            node = self.unfinished.pop()
            parent = self.unfinished[-1]
            parent.children.append(node)
        elif tag in self.SELF_CLOSING_TAGS:
            parent = self.unfinished[-1] if self.unfinished else None
            if parent is None:
                self.implicit_tags(tag); parent = self.unfinished[-1]
            node = Element(tag, attributes, parent)
            parent.children.append(node)
        else:
            parent = self.unfinished[-1] if self.unfinished else None
            node = Element(tag, attributes, parent)
            self.unfinished.append(node)

    def finish(self):
        if not self.unfinished:
            self.implicit_tags(None)
        while len(self.unfinished) > 1:
            node = self.unfinished.pop()
            parent = self.unfinished[-1]
            parent.children.append(node)
        return self.unfinished.pop()

# ================= CSS selectors & cascade =================
class TagSelector:
    def __init__(self, tag):
        self.tag = tag
        self.priority = 1
    def matches(self, node):
        return isinstance(node, Element) and self.tag == node.tag

class DescendantSelector:
    def __init__(self, ancestor, descendant):
        self.ancestor = ancestor
        self.descendant = descendant
        self.priority = ancestor.priority + descendant.priority
    def matches(self, node):
        if not self.descendant.matches(node): return False
        while node.parent:
            if self.ancestor.matches(node.parent): return True
            node = node.parent
        return False

class CSSParser:
    """
    A very small CSS parser used for parsing author style sheets. It
    supports tag and descendant selectors, and property/value pairs. The
    parser skips malformed rules and continues parsing. See Chapter 6
    exercises for details.
    """
    def __init__(self, s: str) -> None:
        self.s = s
        self.i = 0

    def whitespace(self) -> None:
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def literal(self, literal: str) -> None:
        if not (self.i < len(self.s) and self.s[self.i] == literal):
            raise Exception(f"Expected '{literal}'")
        self.i += 1

    def word(self) -> str:
        start = self.i
        while self.i < len(self.s) and (
            self.s[self.i].isalnum() or self.s[self.i] in "#-.%"
        ):
            self.i += 1
        if not (self.i > start):
            raise Exception("Expected word")
        return self.s[start:self.i]

    def ignore_until(self, chars: list[str]) -> str | None:
        while self.i < len(self.s):
            if self.s[self.i] in chars:
                return self.s[self.i]
            self.i += 1
        return None

    def pair(self) -> tuple[str, str]:
        prop = self.word()
        self.whitespace()
        self.literal(":")
        self.whitespace()
        val = self.word()
        return prop.casefold(), val

    def body(self) -> dict[str, str]:
        pairs: dict[str, str] = {}
        while self.i < len(self.s) and self.s[self.i] != "}":
            try:
                prop, val = self.pair()
                pairs[prop] = val
                self.whitespace()
                self.literal(";")
                self.whitespace()
            except Exception:
                why = self.ignore_until([";", "}"])
                if why == ";":
                    self.literal(";")
                    self.whitespace()
                else:
                    break
        return pairs

    def selector(self):
        out = TagSelector(self.word().casefold())
        self.whitespace()
        while self.i < len(self.s) and self.s[self.i] != "{":
            tag = self.word()
            descendant = TagSelector(tag.casefold())
            out = DescendantSelector(out, descendant)
            self.whitespace()
        return out

    def parse(self) -> list[tuple[object, dict[str, str]]]:
        rules: list[tuple[object, dict[str, str]]] = []
        while self.i < len(self.s):
            try:
                self.whitespace()
                selector = self.selector()
                self.literal("{")
                self.whitespace()
                body = self.body()
                self.literal("}")
                rules.append((selector, body))
            except Exception:
                why = self.ignore_until(["}"])
                if why == "}":
                    self.literal("}")
                    self.whitespace()
                else:
                    break
        return rules

def cascade_priority(rule):
    selector, _ = rule
    return selector.priority

# UA stylesheet as Python objects (robust)
DEFAULT_STYLE_SHEET = [
    (TagSelector("body"), {"background-color": "white", "color": "black"}),
    (TagSelector("pre"),  {"background-color": "gray"}),
    (DescendantSelector(TagSelector("body"), TagSelector("a")), {"color": "blue"}),
    # Widgets
    (TagSelector("input"),  {"font-size": "16px", "font-weight": "normal", "font-style": "normal",
                             "background-color": "lightblue", "color": "black"}),
    (TagSelector("button"), {"font-size": "16px", "font-weight": "normal", "font-style": "normal",
                             "background-color": "orange", "color": "black"}),
    (TagSelector("i"),    {"font-style": "italic"}),
    (TagSelector("b"),    {"font-weight": "bold"}),
    (TagSelector("small"),{"font-size": "90%"}),
    (TagSelector("big"),  {"font-size": "110%"}),
]

# ================= Fonts & layout =================
FONTS = {}
def get_font(size, weight, style):
    key = (size, weight, style)
    if key not in FONTS:
        font = tkinter.font.Font(size=size, weight=weight, slant=style)
        label = tkinter.Label(font=font)
        FONTS[key] = (font, label)
    return FONTS[key][0]

WIDTH, HEIGHT = 800, 600
HSTEP, VSTEP = 13, 18
SCROLL_STEP = 100
SCROLLBAR_WIDTH = 12
INPUT_WIDTH_PX = 200
CHECKBOX_SIZE = 16

BLOCK_ELEMENTS = [
    "html","body","article","section","nav","aside",
    "h1","h2","h3","h4","h5","h6","hgroup","header",
    "footer","address","p","hr","pre","blockquote",
    "ol","ul","menu","li","dl","dt","dd","figure",
    "figcaption","main","div","table","form","fieldset",
    "legend","details","summary"
]

INHERITED_PROPERTIES = {
    "font-size": "16px",
    "font-style": "normal",
    "font-weight": "normal",
    "color": "black",
}

def style(node, rules):
    node.style = {}
    for prop, default_value in INHERITED_PROPERTIES.items():
        if node.parent: node.style[prop] = node.parent.style[prop]
        else: node.style[prop] = default_value
    for selector, body in rules:
        if selector.matches(node):
            for p, v in body.items():
                node.style[p] = v
    if node.style["font-size"].endswith("%"):
        parent_px = float((node.parent.style["font-size"] if node.parent else INHERITED_PROPERTIES["font-size"])[:-2])
        node_pct = float(node.style["font-size"][:-1]) / 100
        node.style["font-size"] = str(node_pct * parent_px) + "px"
    for c in node.children:
        style(c, rules)

class DocumentLayout:
    def __init__(self, node):
        self.node = node
        self.parent = None
        self.children = []
        self.x = self.y = self.width = self.height = None
    def layout(self):
        child = BlockLayout(self.node, self, None)
        self.children = [child]
        self.width = WIDTH - 2*HSTEP - SCROLLBAR_WIDTH
        self.x = HSTEP
        self.y = VSTEP
        child.layout()
        self.height = child.height
    def paint(self): return []
    def should_paint(self): return True

class BlockLayout:
    def __init__(self, node, parent, previous):
        self.node = node
        self.parent = parent
        self.previous = previous
        self.children = []
        self.display_list = []  # tuples for inline items
        self.x = self.y = self.width = self.height = None
        self.cursor_x = self.cursor_y = 0
        self.weight = "normal"; self.style = "roman"; self.size = 12
        self.line = []

    def layout_mode(self):
        if isinstance(self.node, Text): return "inline"
        elif any(isinstance(c, Element) and c.tag in BLOCK_ELEMENTS for c in self.node.children): return "block"
        elif self.node.children or (isinstance(self.node, Element) and self.node.tag in ["input","button"]):
            return "inline"
        else: return "block"

    def layout(self):
        self.x = self.parent.x
        self.width = self.parent.width
        self.y = (self.previous.y + self.previous.height) if self.previous else self.parent.y
        mode = self.layout_mode()
        if mode == "block":
            prev = None
            for c in self.node.children:
                child = BlockLayout(c, self, prev)
                self.children.append(child)
                prev = child
        else:
            self.cursor_x = 0; self.cursor_y = 0
            self.line = []
            self.recurse(self.node)
            self.flush()

        for c in self.children:
            c.layout()

        if mode == "block":
            self.height = sum(ch.height for ch in self.children) if self.children else VSTEP
        else:
            # Compute bottom Y from the last drawn primitive, regardless of shape.
            last_y = self.y
            for it in reversed(self.display_list):
                # New tagged formats
                tag = it[0] if isinstance(it, tuple) and it and isinstance(it[0], str) else None
                if tag in ("text", "text_abs"):
                    # ("text_abs", (x, y), word, font, color)
                    last_y = it[1][1]
                    break
                elif tag in ("rect", "outline"):
                    # ("rect"/"outline", (x1,y1,x2,y2), ...)
                    last_y = it[1][3]
                    break
                elif tag == "line":
                    # ("line", (x1,y1,x2,y2,color,th))
                    last_y = max(it[1][1], it[1][3])
                    break
                # Legacy 5-tuple support: (x, y, word, font, color)
                if tag is None and len(it) >= 2 and isinstance(it[1], (int, float)):
                    last_y = it[1]
                    break

            default_font = get_font(12, "normal", "roman")
            self.height = max((last_y - self.y) + default_font.metrics("linespace"), VSTEP)


    def recurse(self, node):
        if isinstance(node, Text):
            for w in node.text.split():
                self.word(node, w)
        else:
            if isinstance(node, Element) and node.tag in ["input","button","br"]:
                if node.tag == "br":
                    self.flush()
                else:
                    self.input(node)
            else:
                for c in node.children:
                    self.recurse(c)

    def word(self, node, word):
        weight = node.style["font-weight"]
        style = node.style["font-style"]
        if style == "normal": style = "roman"
        size = int(float(node.style["font-size"][:-2]) * .75)
        font = get_font(size, weight, style)
        color = node.style["color"]
        w = font.measure(word)
        if self.cursor_x + w > self.width:
            self.flush()
        self.line.append(("text", self.cursor_x, word, font, color))
        self.cursor_x += w + font.measure(" ")

    def input(self, node):
        # compute font used inside widget
        weight = node.style["font-weight"]
        style = node.style["font-style"]
        if style == "normal": style = "roman"
        size = int(float(node.style["font-size"][:-2]) * .75)
        font = get_font(size, weight, style)

        # size by type
        is_checkbox = node.attributes.get("type","text").lower() == "checkbox"
        w = CHECKBOX_SIZE if is_checkbox else (INPUT_WIDTH_PX if node.tag == "input" else max(80, font.measure(self.button_label(node)) + 20))

        if self.cursor_x + w > self.width:
            self.flush()

        metrics = font.metrics()
        max_ascent = metrics["ascent"]
        baseline = self.cursor_y + max_ascent
        x = self.x + self.cursor_x
        y_top = self.y + baseline - font.metrics("ascent")
        y_bottom = y_top + (CHECKBOX_SIZE if is_checkbox else font.metrics("linespace"))
        rect = (x, y_top, x + w, y_bottom)

        # register for hit-testing
        Browser._register_widget_box(node, rect)

        # background/box
        if is_checkbox:
            # draw a square outline; fill light background
            self.display_list.append(("rect", rect, "#e6f2ff"))
            self.display_list.append(("outline", rect, "black", 1))
            # draw check if checked
            checked = ("checked" in node.attributes) or (node.attributes.get("_checked_state") == "true")
            if checked:
                # simple X check
                self.display_list.append(("line", (x+3, y_top+3, x+w-3, y_bottom-3, "black", 2)))
                self.display_list.append(("line", (x+w-3, y_top+3, x+3, y_bottom-3, "black", 2)))
        else:
            bgcolor = node.style.get("background-color", "transparent")
            if bgcolor != "transparent":
                self.display_list.append(("rect", rect, bgcolor))

            if node.tag == "input":
                text = node.attributes.get("value", "")
            else:
                text = self.button_label(node)
            color = node.style["color"]
            self.display_list.append(("text_abs", (x, y_top), text, font, color))
            if node.is_focused and node.tag == "input":
                cx = x + font.measure(text)
                self.display_list.append(("line", (cx, y_top, cx, y_bottom, "black", 1)))

        # advance cursor
        self.cursor_x += w + font.measure(" ")

    def button_label(self, node):
        if len(node.children) == 1 and isinstance(node.children[0], Text):
            return node.children[0].text
        return ""

    def flush(self):
        if not self.line: return
        metrics = [font.metrics() for _, _, _, font, _ in self.line]
        max_ascent = max(m["ascent"] for m in metrics)
        max_descent = max(m["descent"] for m in metrics)
        baseline = self.cursor_y + max_ascent
        for kind, rel_x, word, font, color in self.line:
            x = self.x + rel_x
            y = self.y + baseline - font.metrics("ascent")
            self.display_list.append(("text_abs", (x, y), word, font, color))
        self.cursor_y = baseline + int(1.25 * max_descent)
        self.cursor_x = 0
        self.line = []

    def should_paint(self):
        if isinstance(self.node, Element) and self.node.tag in ["input","button"]:
            return False
        return True

    def paint(self):
        cmds = []
        if isinstance(self.node, Element) and self.node.tag == "pre":
            x2, y2 = self.x + self.width, self.y + self.height
            cmds.append(DrawRect(self.x, self.y, x2, y2, "gray"))
        for item in self.display_list:
            if item[0] == "text_abs":
                _, (x,y), word, font, color = item
                cmds.append(DrawText(x, y, word, font, color))
            elif item[0] == "rect":
                _, (x1,y1,x2,y2), color = item
                cmds.append(DrawRect(x1, y1, x2, y2, color))
            elif item[0] == "line":
                _, (x1,y1,x2,y2,color,th) = item
                cmds.append(DrawLine(x1, y1, x2, y2, color, th))
            elif item[0] == "outline":
                _, (x1,y1,x2,y2), color, th = item
                cmds.append(DrawOutline(x1, y1, x2, y2, color, th))
        return cmds

# ================= Draw commands + geometry shims =================
class Rect:
    def __init__(self, left, top, right, bottom):
        self.left, self.top, self.right, self.bottom = left, top, right, bottom
    def contains_point(self, x, y):
        return self.left <= x <= self.right and self.top <= y <= self.bottom

class DrawText:
    def __init__(self, x1, y1, text, font, color):
        self.top = y1; self.left = x1
        self.text = text; self.font = font; self.color = color
    def execute(self, scroll, canvas):
        canvas.create_text(self.left, self.top - scroll,
                           text=self.text, font=self.font,
                           fill=self.color, anchor='nw')

class DrawRect:
    def __init__(self, x1, y1, x2, y2, color):
        self.top = y1; self.left = x1
        self.bottom = y2; self.right = x2
        self.color = color
    def execute(self, scroll, canvas):
        canvas.create_rectangle(self.left, self.top - scroll,
                                self.right, self.bottom - scroll,
                                width=0, fill=self.color)

class DrawLine:
    def __init__(self, x1, y1, x2, y2, color, thickness=1):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.color = color; self.thickness = thickness
    def execute(self, scroll, canvas):
        canvas.create_line(self.x1, self.y1 - scroll, self.x2, self.y2 - scroll,
                           fill=self.color, width=self.thickness)

class DrawOutline:
    def __init__(self, x1, y1, x2, y2, color, thickness=1):
        self.x1, self.y1, self.x2, self.y2 = x1, y1, x2, y2
        self.color = color; self.thickness = thickness
    def execute(self, scroll, canvas):
        canvas.create_rectangle(self.x1, self.y1 - scroll, self.x2, self.y2 - scroll,
                                outline=self.color, width=self.thickness)

# ================= JavaScript runtime & context =================
# The following strings define a minimal DOM-like API implemented in
# JavaScript. They expose methods used by exercises in Chapter 9
# such as Node.children, document.createElement, appendChild,
# insertBefore, removeChild, and event bubbling with
# stopPropagation. The Python functions registered with DukPy
# provide the backing functionality for these methods.
RUNTIME_JS = """
function Node(handle) { this.handle = handle; }
var LISTENERS = {};
Node.prototype.addEventListener = function(type, listener) {
  if (!LISTENERS[this.handle]) LISTENERS[this.handle] = {};
  var dict = LISTENERS[this.handle];
  if (!dict[type]) dict[type] = [];
  dict[type].push(listener);
};
// dispatchEvent handles event bubbling. It calls listeners on this
// node, then recurses up the tree if the event hasn’t been stopped.
Node.prototype.dispatchEvent = function(evt) {
  var list = (LISTENERS[this.handle] && LISTENERS[this.handle][evt.type]) || [];
  for (var i = 0; i < list.length; i++) {
    list[i].call(this, evt);
  }
  var do_default = evt.do_default;
  var do_bubble = evt.do_bubble;
  if (do_bubble) {
    var parentHandle = call_python("getParent", this.handle);
    if (parentHandle != -1) {
      var parent = new Node(parentHandle);
      // propagate; merge default flags so that preventDefault anywhere
      // stops the default
      do_default = parent.dispatchEvent(evt) && do_default;
    }
  }
  return do_default;
};
function Event(type) {
  this.type = type;
  this.do_default = true;
  this.do_bubble = true;
}
Event.prototype.preventDefault = function() { this.do_default = false; };
Event.prototype.stopPropagation = function() { this.do_bubble = false; };
// document.querySelectorAll forwards to Python to find matching
// elements; returns an array of Node objects.
document = {
  querySelectorAll: function(sel) {
    var handles = call_python("querySelectorAll", sel.toString());
    var out = [];
    for (var i = 0; i < handles.length; i++) {
      out.push(new Node(handles[i]));
    }
    return out;
  }
};
// Create elements in the document; implemented in Python.
document.createElement = function(tag) {
  var h = call_python("create_element", tag.toString().toLowerCase());
  return new Node(h);
};
// Expose Node.children property: immediate element children only
Object.defineProperty(Node.prototype, "children", {
  get: function() {
    var handles = call_python("children", this.handle);
    var out = [];
    for (var i = 0; i < handles.length; i++) {
      out.push(new Node(handles[i]));
    }
    return out;
  }
});
// Node.innerHTML getter/setter
Object.defineProperty(Node.prototype, "innerHTML", {
  get: function() {
    return call_python("innerHTML_get", this.handle);
  },
  set: function(value) {
    call_python("innerHTML_set", this.handle, value.toString());
  }
});
// Node.outerHTML getter
Object.defineProperty(Node.prototype, "outerHTML", {
  get: function() {
    return call_python("outerHTML_get", this.handle);
  }
});
// Node.id property; forwards to getAttribute/set_attribute
Object.defineProperty(Node.prototype, "id", {
  get: function() {
    return call_python("getAttribute", this.handle, "id");
  },
  set: function(value) {
    call_python("set_attribute", this.handle, "id", value.toString());
  }
});
Node.prototype.getAttribute = function(attr) {
  return call_python("getAttribute", this.handle, attr.toString());
};
Node.prototype.setAttribute = function(attr, val) {
  call_python("set_attribute", this.handle, attr.toString(), val.toString());
};
// Node.appendChild inserts a child at the end of children
Node.prototype.appendChild = function(child) {
  call_python("append_child", this.handle, child.handle);
  return child;
};
// Node.insertBefore inserts a child before the reference node
Node.prototype.insertBefore = function(child, ref) {
  call_python("insert_before", this.handle, child.handle, ref.handle);
  return child;
};
// Node.removeChild detaches a child from this node
Node.prototype.removeChild = function(child) {
  call_python("remove_child", this.handle, child.handle);
  return child;
};
"""

# When dispatching an event from Python, we call this snippet. It
# constructs a new Event and dispatches it on a Node, returning
# true if the default action should run and false if it should be
# prevented. The Python side will invert this to determine whether
# to skip the default action.
EVENT_DISPATCH_JS = "new Node(dukpy.handle).dispatchEvent(new Event(dukpy.type))"

class JSContext:
    """
    A JavaScript execution context based on DukPy. It provides a
    minimal DOM API for JavaScript code running in the browser. The
    context maintains mappings between Python DOM nodes and numeric
    handles used in JavaScript. It also exports several Python
    functions to JavaScript via `call_python`.
    """
    def __init__(self, tab: 'Tab') -> None:
        self.tab = tab
        if dukpy is None:
            raise RuntimeError("DukPy is required for JavaScript support")
        self.interp = dukpy.JSInterpreter()
        # Mapping between Python nodes and JS handles
        self.node_to_handle: dict[object, int] = {}
        self.handle_to_node: dict[int, object] = {}
        # Export Python functions
        self.interp.export_function("querySelectorAll", self.querySelectorAll)
        self.interp.export_function("getAttribute", self.getAttribute)
        self.interp.export_function("innerHTML_set", self.innerHTML_set)
        self.interp.export_function("children", self.children)
        self.interp.export_function("create_element", self.create_element)
        self.interp.export_function("append_child", self.append_child)
        self.interp.export_function("insert_before", self.insert_before)
        self.interp.export_function("remove_child", self.remove_child)
        self.interp.export_function("getParent", self.getParent)
        self.interp.export_function("innerHTML_get", self.innerHTML_get)
        self.interp.export_function("outerHTML_get", self.outerHTML_get)
        self.interp.export_function("set_attribute", self.set_attribute)
        # Load runtime script
        self.interp.evaljs(RUNTIME_JS)
        # Track id variables defined in JS
        self.id_vars: list[str] = []

    # ----- handle management -----
    def get_handle(self, elt) -> int:
        """Return a stable handle for a Python node, creating one if needed."""
        if elt not in self.node_to_handle:
            h = len(self.node_to_handle)
            self.node_to_handle[elt] = h
            self.handle_to_node[h] = elt
        return self.node_to_handle[elt]

    # ----- exported functions -----
    def querySelectorAll(self, selector_text: str) -> list[int]:
        # Return handles for all nodes matching a CSS selector.
        try:
            selector = CSSParser(selector_text).selector()
        except Exception:
            return []
        nodes = [n for n in tree_to_list(self.tab.nodes, []) if selector.matches(n)]
        return [self.get_handle(n) for n in nodes]

    def getAttribute(self, handle: int, attr: str) -> str:
        node = self.handle_to_node.get(handle)
        if isinstance(node, Element):
            return node.attributes.get(attr, "")
        return ""

    def set_attribute(self, handle: int, attr: str, value: str) -> None:
        node = self.handle_to_node.get(handle)
        if not isinstance(node, Element):
            return
        # Update attribute
        if value is None:
            if attr in node.attributes:
                del node.attributes[attr]
        else:
            node.attributes[attr] = value
        # Update id variables if id changed
        if attr == "id":
            self.update_ids()
        # Re-style and re-render because attributes may change styling
        # For script/link src changes, process scripts and styles
        self.tab.process_scripts_and_styles()
        # Recompute style rules and layout
        self.tab.apply_styles_and_render()

    def innerHTML_set(self, handle: int, s: str) -> None:
        # Replace children of node with new HTML
        node = self.handle_to_node.get(handle)
        if not isinstance(node, Element):
            return
        # Parse the new HTML; wrap in a dummy element to parse children
        try:
            parsed = HTMLParser("<body>" + s + "</body>").parse()
        except Exception:
            return
        new_children = parsed.children  # children under body
        # Detach existing children
        node.children = []
        for c in new_children:
            c.parent = node
        node.children = new_children
        # Update stylesheets and scripts; restyle and render
        self.tab.process_scripts_and_styles()
        self.tab.apply_styles_and_render()
        # Update id variables
        self.update_ids()

    def innerHTML_get(self, handle: int) -> str:
        node = self.handle_to_node.get(handle)
        if node is None:
            return ""
        out = []
        for child in getattr(node, "children", []):
            out.append(self._serialize(child))
        return "".join(out)

    def outerHTML_get(self, handle: int) -> str:
        node = self.handle_to_node.get(handle)
        if node is None:
            return ""
        return self._serialize(node)

    def _serialize(self, node) -> str:
        # Convert a node subtree back into HTML
        if isinstance(node, Text):
            return node.text
        if isinstance(node, Element):
            attrs = []
            for k, v in node.attributes.items():
                if v == "":
                    attrs.append(k)
                else:
                    # quote attribute values with double quotes
                    # and escape double quotes inside
                    val = v.replace('"', '&quot;')
                    attrs.append(f'{k}="{val}"')
            attr_str = (" " + " ".join(attrs)) if attrs else ""
            # Self-closing tags
            if node.tag in HTMLParser.SELF_CLOSING_TAGS:
                return f"<{node.tag}{attr_str}>"
            inner = []
            for c in node.children:
                inner.append(self._serialize(c))
            inner_str = "".join(inner)
            return f"<{node.tag}{attr_str}>" + inner_str + f"</{node.tag}>"
        return ""

    def children(self, handle: int) -> list[int]:
        node = self.handle_to_node.get(handle)
        out: list[int] = []
        if isinstance(node, Element):
            for c in node.children:
                if isinstance(c, Element):
                    out.append(self.get_handle(c))
        return out

    def create_element(self, tag: str) -> int:
        # Create a detached Element. It will be inserted later.
        new_node = Element(tag, {}, None)
        # Default style based on inheritance (will be updated when inserted)
        new_node.style = {k: v for k, v in INHERITED_PROPERTIES.items()}
        return self.get_handle(new_node)

    def append_child(self, parent_handle: int, child_handle: int) -> None:
        parent = self.handle_to_node.get(parent_handle)
        child = self.handle_to_node.get(child_handle)
        if not (isinstance(parent, Element) and child):
            return
        # Detach child from old parent if present
        if hasattr(child, "parent") and child.parent is not None:
            try:
                child.parent.children.remove(child)
            except ValueError:
                pass
        child.parent = parent
        parent.children.append(child)
        # Process potential scripts/styles and restyle DOM
        self.tab.process_scripts_and_styles()
        self.tab.apply_styles_and_render()
        # Update id variables
        self.update_ids()

    def insert_before(self, parent_handle: int, child_handle: int, ref_handle: int) -> None:
        parent = self.handle_to_node.get(parent_handle)
        child = self.handle_to_node.get(child_handle)
        ref = self.handle_to_node.get(ref_handle)
        if not (isinstance(parent, Element) and child and ref):
            return
        # Detach child from old parent if present
        if hasattr(child, "parent") and child.parent is not None:
            try:
                child.parent.children.remove(child)
            except ValueError:
                pass
        child.parent = parent
        try:
            idx = parent.children.index(ref)
        except ValueError:
            parent.children.append(child)
        else:
            parent.children.insert(idx, child)
        # Update
        self.tab.process_scripts_and_styles()
        self.tab.apply_styles_and_render()
        self.update_ids()

    def remove_child(self, parent_handle: int, child_handle: int) -> None:
        parent = self.handle_to_node.get(parent_handle)
        child = self.handle_to_node.get(child_handle)
        if not (isinstance(parent, Element) and child):
            return
        try:
            parent.children.remove(child)
        except ValueError:
            return
        child.parent = None
        # Remove any style sheets associated with removed subtree
        self.tab.process_scripts_and_styles()
        self.tab.apply_styles_and_render()
        self.update_ids()

    def getParent(self, handle: int) -> int:
        node = self.handle_to_node.get(handle)
        if hasattr(node, "parent") and node.parent is not None:
            return self.get_handle(node.parent)
        return -1

    # ----- high-level operations -----
    def update_ids(self) -> None:
        """Update global variables in the JS interpreter for element IDs."""
        if dukpy is None:
            return
        # Clear previous id vars
        for var in self.id_vars:
            try:
                self.interp.evaljs(f"{var} = undefined;")
            except Exception:
                pass
        self.id_vars = []
        # Recreate variables for current elements
        nodes = tree_to_list(self.tab.nodes, []) if self.tab.nodes else []
        for node in nodes:
            if isinstance(node, Element) and "id" in node.attributes:
                varname = node.attributes["id"]
                # Only allow identifiers that start with a letter or underscore
                if not varname or not (varname[0].isalpha() or varname[0] == "_"):
                    continue
                handle = self.get_handle(node)
                try:
                    self.interp.evaljs(f"var {varname} = new Node({handle});")
                    self.id_vars.append(varname)
                except Exception:
                    continue

    def run(self, code: str) -> None:
        """Execute JavaScript code in this context."""
        try:
            self.interp.evaljs(code)
        except Exception as ex:
            # Ignore script errors to avoid crashing the browser
            print("JS error:", ex)

    def dispatch_event(self, type: str, elt) -> bool:
        """Dispatch an event of the given type on the given element.
        Returns True if the default action should be skipped (prevented)."""
        handle = self.node_to_handle.get(elt)
        if handle is None:
            return False
        try:
            # Node.dispatchEvent returns true if default should run
            do_default = self.interp.evaljs(EVENT_DISPATCH_JS, type=type, handle=handle)
        except Exception:
            return False
        return not bool(do_default)

def paint_tree(layout_object, display_list):
    if hasattr(layout_object, "should_paint") and not layout_object.should_paint():
        pass
    else:
        display_list.extend(layout_object.paint())
    for child in layout_object.children:
        paint_tree(child, display_list)

# ================= Tab abstraction =================
class Tab:
    def __init__(self, browser, home_url=None):
        self.browser = browser
        self.history = []            # list of dicts: {url, method, body}
        self.history_index = -1
        self.nodes = None
        self.document = None
        self.display_list = []
        self.scroll = 0
        self.doc_height = HEIGHT
        self.title = "New Tab"
        self.focus = None            # focused input Element
        self.url = None              # current page URL
        self.js = None               # JavaScript context for this tab
        self.loaded_scripts: set[str] = set()
        self.loaded_styles: dict[object, list] = {}
        self.extra_style_rules: list[tuple[object, dict[str, str]]] = []
        if home_url: self.navigate(home_url)

    def navigate(self, url, method="GET", body=None):
        # trim forward history
        if self.history_index + 1 < len(self.history):
            self.history = self.history[:self.history_index + 1]
        self.history.append({"url": url, "method": method, "body": body})
        self.history_index += 1
        self.load(url, payload=(body if method == "POST" else None))

    def go_back(self):
        if self.history_index > 0:
            self.history_index -= 1
            self._restore_history_entry()

    def go_forward(self):
        if self.history_index + 1 < len(self.history):
            self.history_index += 1
            self._restore_history_entry()

    def reload(self):
        if 0 <= self.history_index < len(self.history):
            entry = self.history[self.history_index]
            # 8-5: Do not re-POST on reload; reload with GET
            self.load(entry["url"], payload=None)

    def _restore_history_entry(self):
        entry = self.history[self.history_index]
        # 8-5 safety: never auto re-POST on history nav; do GET instead
        self.load(entry["url"], payload=None)

    def load(self, url, payload=None):
        try:
            self.browser.set_status("Loading…")
            body = url.request(payload)
            self.browser.set_status("")
        except Exception as ex:
            self.browser.set_status(f"Network error: {ex}")
            return
        self.url = url
        self.nodes = HTMLParser(body).parse()
        self.title = self._extract_title() or f"{url.host}"
        # Initialize JavaScript context
        if dukpy is not None:
            try:
                self.js = JSContext(self)
            except Exception as ex:
                print("Failed to initialize JSContext:", ex)
                self.js = None
        else:
            self.js = None
        # Reset loaded scripts/styles
        self.loaded_scripts = set()
        self.loaded_styles = {}
        self.extra_style_rules = []
        # Process scripts and styles before styling/layout
        self.process_scripts_and_styles()
        # Apply styles and layout
        self.apply_styles_and_render()
        # Update id variables for JS
        if self.js:
            self.js.update_ids()
        # Update address bar and tab UI
        if self is self.browser.current_tab():
            self.browser.address.delete(0, "end")
            self.browser.address.insert(0, str(url))
            self.browser.draw()
            self.browser.refresh_tab_strip()

    def render(self):
        Browser._clear_widget_boxes()
        self.document = DocumentLayout(self.nodes)
        self.document.layout()
        self.display_list = []
        paint_tree(self.document, self.display_list)
        self.doc_height = self.document.height
        self.scroll = min(self.scroll, max(0, self.doc_height - HEIGHT))

    def _extract_title(self):
        def walk(n):
            if isinstance(n, Element) and n.tag == "title":
                buf = []
                def collect(t):
                    if isinstance(t, Text): buf.append(t.text.strip())
                    for c in t.children: collect(c)
                collect(n)
                return " ".join(x for x in buf if x)
            for c in n.children:
                r = walk(c)
                if r: return r
            return None
        return walk(self.nodes) if self.nodes else None

    def clamp_scroll(self):
        self.scroll = max(0, min(self.scroll, max(0, self.doc_height - HEIGHT)))

    def scrolldown(self, step=SCROLL_STEP):
        self.scroll += step; self.clamp_scroll()

    def scrollup(self, step=SCROLL_STEP):
        self.scroll -= step; self.clamp_scroll()

    # ---- input focus, typing, clicking ----
    def click(self, x, y):
        doc_y = y + self.scroll
        elt = Browser._hit_widget(x, doc_y)

        # blur previous focus
        self.blur()

        if elt is not None:
            # Dispatch click event to JS; if prevented, return
            if self.js:
                try:
                    prevent = self.js.dispatch_event("click", elt)
                except Exception:
                    prevent = False
                if prevent:
                    # JS cancelled default
                    self.apply_styles_and_render()
                    return
            if elt.tag == "input":
                # checkbox click toggles; text input focuses
                if elt.attributes.get("type","text").lower() == "checkbox":
                    # toggle internal checked state
                    if ("checked" in elt.attributes) or (elt.attributes.get("_checked_state") == "true"):
                        # uncheck
                        if "checked" in elt.attributes: del elt.attributes["checked"]
                        elt.attributes["_checked_state"] = "false"
                    else:
                        elt.attributes["_checked_state"] = "true"
                    # Rerender after toggle
                    self.apply_styles_and_render()
                    return
                # text input focus & clear
                elt.attributes["value"] = ""
                self.focus = elt
                elt.is_focused = True
                self.apply_styles_and_render()
                return
            elif elt.tag == "button":
                form = elt.parent
                while form and not (isinstance(form, Element) and form.tag == "form"):
                    form = form.parent
                if form:
                    self.submit_form(form)
                    return

        self.apply_styles_and_render()

    def keypress(self, char):
        if self.focus and isinstance(self.focus, Element) and self.focus.tag == "input":
            # Dispatch keydown event; if prevented, skip default behaviour
            prevent = False
            if self.js:
                try:
                    prevent = self.js.dispatch_event("keydown", self.focus)
                except Exception:
                    prevent = False
            if prevent:
                self.apply_styles_and_render()
                return
            if char == "\r" or char == "\n":
                # 8-1: Enter submits enclosing form
                form = self.focus.parent
                while form and not (isinstance(form, Element) and form.tag == "form"):
                    form = form.parent
                if form:
                    self.submit_form(form)
                return
            # simple text append (backspace etc. omitted for brevity)
            self.focus.attributes["value"] = self.focus.attributes.get("value", "") + char
            self.apply_styles_and_render()

    def submit_form(self, form_elt):
        # Dispatch submit event to JS; skip default if prevented
        prevent = False
        if self.js:
            try:
                prevent = self.js.dispatch_event("submit", form_elt)
            except Exception:
                prevent = False
        if prevent:
            self.apply_styles_and_render()
            return
        # Collect inputs
        inputs = [n for n in tree_to_list(form_elt, [])
                  if isinstance(n, Element) and n.tag == "input" and "name" in n.attributes]

        parts = []
        for inp in inputs:
            itype = inp.attributes.get("type","text").lower()
            if itype == "checkbox":
                checked = ("checked" in inp.attributes) or (inp.attributes.get("_checked_state") == "true")
                if not checked:
                    continue  # include only if checked
                name = urllib.parse.quote(inp.attributes["name"])
                value = urllib.parse.quote(inp.attributes.get("value","on"))
                parts.append(f"{name}={value}")
            else:
                name = urllib.parse.quote(inp.attributes["name"])
                value = urllib.parse.quote(inp.attributes.get("value","") )
                parts.append(f"{name}={value}")
        body = "&".join(parts)

        action = form_elt.attributes.get("action","")
        url = self.url.resolve(action)
        # record as POST in history (8-5)
        self.navigate(url, method="POST", body=body)

    def blur(self):
        # 8-3: clear tab focus & caret
        if self.focus:
            self.focus.is_focused = False
            self.focus = None

    # -------- script/style processing --------
    def process_scripts_and_styles(self) -> None:
        """
        Scan the current DOM for <script> and <link rel="stylesheet"> tags.
        Newly added script tags are fetched and executed. For <link>
        style sheets, rules are loaded via CSSParser. Removed
        <link> elements have their style rules dropped. This method
        updates self.loaded_scripts, self.loaded_styles and
        self.extra_style_rules accordingly. It should be called
        before styling and layout.
        """
        # Only do anything if we have a DOM
        if not self.nodes:
            return
        # Build a new mapping of link elements to style rules
        new_loaded_styles: dict[object, list] = {}
        # Traverse all nodes
        for node in tree_to_list(self.nodes, []):
            if isinstance(node, Element):
                # Process <script src="...">
                if node.tag == "script" and "src" in node.attributes:
                    src = node.attributes["src"]
                    # Avoid executing the same script twice
                    if src not in self.loaded_scripts and self.js:
                        try:
                            script_url = self.url.resolve(src)
                            body = script_url.request()
                            # Run script inside JSContext
                            try:
                                self.js.run(body)
                            except Exception:
                                pass
                            self.loaded_scripts.add(src)
                        except Exception:
                            # Network error: ignore
                            pass
                # Process <link rel="stylesheet" href="...">
                if node.tag == "link" and node.attributes.get("rel", "").casefold() == "stylesheet" and "href" in node.attributes:
                    href = node.attributes["href"]
                    # Fetch and parse CSS for new or changed links
                    if node not in self.loaded_styles:
                        try:
                            css_url = self.url.resolve(href)
                            css_text = css_url.request()
                            parser = CSSParser(css_text)
                            rules = parser.parse()
                        except Exception:
                            rules = []
                        new_loaded_styles[node] = rules
                    else:
                        # keep existing rules if not removed
                        new_loaded_styles[node] = self.loaded_styles[node]
        # Update loaded_styles and compute extra_style_rules
        self.loaded_styles = new_loaded_styles
        extra: list[tuple[object, dict[str, str]]] = []
        for rules in self.loaded_styles.values():
            extra.extend(rules)
        self.extra_style_rules = extra

    def apply_styles_and_render(self) -> None:
        """
        Apply CSS styles to the DOM and render the page. Combines
        DEFAULT_STYLE_SHEET with any extra style rules loaded from
        <link> elements. After styling, lays out the document and
        paints it.
        """
        if not self.nodes:
            return
        # Compose style rules
        rules = DEFAULT_STYLE_SHEET + self.extra_style_rules
        # Sort by cascade priority
        rules.sort(key=cascade_priority)
        # Apply styles
        style(self.nodes, rules)
        # Layout and paint
        self.render()
        # If this tab is active, redraw the browser
        if self is self.browser.current_tab():
            try:
                self.browser.draw()
            except Exception:
                pass

# ================= Chrome shim (optional) =================
class Chrome:
    def __init__(self, browser):
        self.browser = browser
        self.focus = None
        self.bottom = 0
    def tab_rect(self, i):
        x0 = 6 + i * 140
        return Rect(x0, 2, x0 + 128, 28)
    def draw(self): pass
    def click(self, x, y):
        for i in range(len(self.browser.tabs)):
            r = self.tab_rect(i)
            if r.contains_point(x, y):
                self.browser.switch_tab(i); return
    def keypress(self, char):
        if self.focus == "address bar":
            self.browser.address.insert("end", char)
            return True
        return False
    def enter(self):
        self.browser.go_address()
    def blur(self):
        self.focus = None

# ================= Browser (chrome + tabs) =================
class Browser:
    _widget_boxes = []  # (rect, element)
    @classmethod
    def _register_widget_box(cls, element, rect_tuple):
        x1,y1,x2,y2 = rect_tuple
        cls._widget_boxes.append((Rect(x1,y1,x2,y2), element))
    @classmethod
    def _clear_widget_boxes(cls):
        cls._widget_boxes = []
    @classmethod
    def _hit_widget(cls, x, y):
        for r, elt in reversed(cls._widget_boxes):
            if r.contains_point(x, y):
                return elt
        return None

    def __init__(self):
        self.window = tkinter.Tk()
        self.chrome_ctl = Chrome(self)

        # --- tab strip ---
        self.tabbar = tkinter.Frame(self.window, bg="#e6e6e6")
        self.tabbar.pack(fill="x")
        self.tabs = []
        self.active_tab_index = 0

        # --- chrome bar ---
        self.chrome = tkinter.Frame(self.window)
        self.back_btn  = tkinter.Button(self.chrome, text="◀", width=2, command=self.go_back)
        self.fwd_btn   = tkinter.Button(self.chrome, text="▶", width=2, command=self.go_forward)
        self.reload_btn= tkinter.Button(self.chrome, text="⟳", width=2, command=self.reload)
        self.address   = tkinter.Entry(self.chrome, width=60)
        self.go_btn    = tkinter.Button(self.chrome, text="Go", command=self.go_address)
        self.back_btn.pack(side="left")
        self.fwd_btn.pack(side="left")
        self.reload_btn.pack(side="left")
        self.address.pack(side="left", fill="x", expand=True, padx=4)
        self.go_btn.pack(side="left")
        self.chrome.pack(fill="x")
        self.chrome_ctl.bottom = self.chrome.winfo_reqheight() + self.tabbar.winfo_reqheight()

        # scrollbar state
        self._dragging_scroll = False
        self._drag_offset = 0
        self.scrollbar_thumb = None
        self._scroll_velocity = 0.0
        self._scroll_animating = False

        # --- canvas ---
        self.canvas = tkinter.Canvas(self.window, width=WIDTH, height=HEIGHT,
                                     background="white", highlightthickness=0)
        self.canvas.pack()

        # --- status ---
        self.status = tkinter.Label(self.window, text="", anchor="w")
        self.status.pack(fill="x")

        # bindings
        self.window.bind("<Return>", lambda e: self.handle_enter())
        self.window.bind("<Down>",   lambda e: self.scroll_active(+SCROLL_STEP))
        self.window.bind("<Up>",     lambda e: self.scroll_active(-SCROLL_STEP))
        self.window.bind("<Prior>",  lambda e: self.scroll_active(-int(HEIGHT*0.9)))
        self.window.bind("<Next>",   lambda e: self.scroll_active(+int(HEIGHT*0.9)))
        self.window.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Button-4>", self.on_wheel_linux)
        self.canvas.bind("<Button-5>", self.on_wheel_linux)
        self.canvas.bind("<Button-1>", self.handle_click)
        self.canvas.bind("<B1-Motion>", self.handle_drag)
        self.canvas.bind("<ButtonRelease-1>", self.handle_release)
        self.window.bind("<Key>", self.handle_key)

        # keyboard shortcuts (Ctrl/Cmd)
        self._bind_accels()

        # first tab
        self.new_tab(URL("https://browser.engineering/chrome.html"))

    # -------- accelerators --------
    def _bind_accels(self):
        def bind_combo(key, handler):
            self.window.bind(f"<Control-{key}>", handler)
            self.window.bind(f"<Command-{key}>", handler)
        bind_combo("t", lambda e: self.new_tab(URL("https://example.org/")))
        bind_combo("w", lambda e: self.close_tab(self.active_tab_index))
        bind_combo("l", lambda e: (self.address.focus_set(), self.address.selection_range(0, "end")))
        def next_tab(e=None):
            if self.tabs:
                self.switch_tab((self.active_tab_index + 1) % len(self.tabs))
        def prev_tab(e=None):
            if self.tabs:
                self.switch_tab((self.active_tab_index - 1) % len(self.tabs))
        self.window.bind("<Control-Tab>", lambda e: next_tab())
        self.window.bind("<Control-Shift-Tab>", lambda e: prev_tab())
        self.window.bind("<Command-Right>", lambda e: next_tab())
        self.window.bind("<Command-Left>",  lambda e: prev_tab())

    # -------- tabs --------
    def current_tab(self) -> Tab:
        return self.tabs[self.active_tab_index]

    def new_tab(self, url: URL):
        tab = Tab(self)
        self.tabs.append(tab)
        self.active_tab_index = len(self.tabs) - 1
        self.refresh_tab_strip()
        if url:
            tab.navigate(url)
        self.draw()

    def switch_tab(self, idx: int):
        if 0 <= idx < len(self.tabs):
            self.active_tab_index = idx
            tab = self.current_tab()
            if 0 <= tab.history_index < len(tab.history):
                url = tab.history[tab.history_index]["url"]
                self.address.delete(0, "end")
                self.address.insert(0, str(url))
            self.refresh_tab_strip()
            self.draw()

    def close_tab(self, idx: int):
        if len(self.tabs) <= 1:
            try:
                self.window.quit()
            finally:
                self.window.destroy()
            return
        del self.tabs[idx]
        if self.active_tab_index >= len(self.tabs):
            self.active_tab_index = len(self.tabs) - 1
        self.refresh_tab_strip()
        self.draw()

    def refresh_tab_strip(self):
        for w in self.tabbar.winfo_children(): w.destroy()
        for i, t in enumerate(self.tabs):
            cell = tkinter.Frame(self.tabbar, bd=0, relief="flat", bg="#e6e6e6")
            title = t.title or "New Tab"
            title_txt = title[:24] + ("…" if len(title) > 24 else "")
            b = tkinter.Button(cell, text=title_txt,
                               command=lambda j=i: self.switch_tab(j),
                               relief="sunken" if i == self.active_tab_index else "raised")
            b.pack(side="left", padx=(2,2), pady=2)
            xbtn = tkinter.Button(cell, text="×", width=2,
                                  command=lambda j=i: self.close_tab(j))
            xbtn.pack(side="left", padx=(2,4), pady=2)
            cell.pack(side="left")
        plus = tkinter.Button(self.tabbar, text="+", width=3,
                              command=lambda: self.new_tab(URL("https://example.org/")))
        plus.pack(side="left", padx=4, pady=2)

    # -------- focus & events --------
    def handle_click(self, e):
        # Scrollbar hit-test first
        track_left = WIDTH - SCROLLBAR_WIDTH
        if e.x >= track_left:
            tab = self.current_tab()
            if tab.doc_height > HEIGHT and self.scrollbar_thumb:
                x1, y1, x2, y2 = self.scrollbar_thumb
                if y1 <= e.y <= y2:
                    # Start dragging the thumb
                    self._dragging_scroll = True
                    self._drag_offset = e.y - y1
                else:
                    # Clicked track: jump thumb (page jump)
                    thumb_h = y2 - y1
                    new_y = max(0, min(e.y - thumb_h // 2, HEIGHT - thumb_h))
                    ratio = new_y / (HEIGHT - thumb_h)
                    tab.scroll = int(ratio * (tab.doc_height - HEIGHT))
                    self.draw()
            return  # don’t forward to page content when clicking the track

        # Existing page-click behavior
        self.address.selection_clear()
        self.address.icursor("end")
        self.chrome_ctl.blur()
        self.current_tab().blur()
        self.current_tab().click(e.x, e.y)
        self.draw()

    def handle_drag(self, e):
        if not self._dragging_scroll or not self.scrollbar_thumb:
            return
        tab = self.current_tab()
        x1, y1, x2, y2 = self.scrollbar_thumb
        thumb_h = y2 - y1
        # Constrain thumb within track
        new_y = max(0, min(e.y - self._drag_offset, HEIGHT - thumb_h))
        ratio = new_y / (HEIGHT - thumb_h)
        tab.scroll = int(ratio * (tab.doc_height - HEIGHT))
        self.draw()

    def handle_release(self, e):
        self._dragging_scroll = False


    def handle_key(self, e):
        widget = self.window.focus_get()
        if widget is self.address:
            # address bar focused; page must be blurred
            self.chrome_ctl.focus = "address bar"
            self.current_tab().blur()  # 8-3
            return
        self.chrome_ctl.focus = None
        if e.char:
            self.current_tab().keypress(e.char)
            self.draw()

    def handle_enter(self):
        widget = self.window.focus_get()
        if widget is self.address:
            self.go_address()
        else:
            # enter in page is handled by Tab.keypress
            pass

    # -------- chrome actions --------
    def set_status(self, msg): self.status.config(text=msg)

    def go_address(self):
        # blur page when switching to navigation via address bar
        self.current_tab().blur()  # 8-3
        url_str = self.address.get().strip()
        if not url_str: return
        if "://" not in url_str:
            url_str = "https://" + url_str
        self.current_tab().navigate(URL(url_str))

    def go_back(self):   self.current_tab().go_back()
    def go_forward(self):self.current_tab().go_forward()
    def reload(self):    self.current_tab().reload()

    # -------- scrolling --------
    def scroll_active(self, delta):
        tab = self.current_tab()
        if delta >= 0: tab.scrolldown(delta)
        else: tab.scrollup(-delta)
        self.draw()

    def on_wheel(self, e):
        # Normalize wheel delta to "pixels" of scroll
        if sys.platform == "darwin":
            # macOS gives small deltas; invert so down = positive scroll
            step = -float(e.delta) * 4.0     # tweak 3.0–6.0 to taste
        else:
            # Windows typically +/-120 per notch
            step = -int(e.delta / 120) * 40  # 40–60 feels good
        self._enqueue_scroll(step)

    def on_wheel_linux(self, e):
        step = -40 if e.num == 4 else +40
        self._enqueue_scroll(step)

    def _enqueue_scroll(self, step):
        # Accumulate velocity and kick the animation loop
        self._scroll_velocity += step
        if not self._scroll_animating:
            self._scroll_animating = True
            self._scroll_tick()

    def _scroll_tick(self):
        # Apply a chunk of velocity each frame, then decay
        # Split big velocities into multiple smaller scrollActive calls
        v = self._scroll_velocity
        # Nothing left? stop.
        if abs(v) < 0.5:
            self._scroll_velocity = 0.0
            self._scroll_animating = False
            return

        # Apply an integer step this frame
        step = int(v)
        if step != 0:
            self.scroll_active(step)

        # Decay velocity for smooth easing-out
        self._scroll_velocity = v * 0.85   # 0.80–0.92: lower = more damping

        # Schedule next frame (~60fps)
        self.window.after(16, self._scroll_tick)


    # -------- painting --------
    def draw(self):
        tab = self.current_tab()
        self.canvas.delete("all")
        for cmd in tab.display_list:
            cmd.execute(tab.scroll, self.canvas)
        self.draw_scrollbar(tab)

    def draw_scrollbar(self, tab: Tab):
        track_left = WIDTH - SCROLLBAR_WIDTH
        self.canvas.create_rectangle(track_left, 0, WIDTH, HEIGHT, width=0, fill="#f0f0f0")
        if tab.doc_height <= HEIGHT:
            self.scrollbar_thumb = None
            return
        ratio = HEIGHT / tab.doc_height
        thumb_h = max(30, int(HEIGHT * ratio))
        max_scroll = tab.doc_height - HEIGHT
        thumb_y = int((tab.scroll / max_scroll) * (HEIGHT - thumb_h))
        self.scrollbar_thumb = (track_left, thumb_y, WIDTH, thumb_y + thumb_h)
        self.canvas.create_rectangle(*self.scrollbar_thumb, width=1, outline="#bbb", fill="#ccc")

# ================= CLI =================
if __name__ == "__main__":
    app = Browser()
    if len(sys.argv) == 2:
        app.current_tab().navigate(URL(sys.argv[1]))
    tkinter.mainloop()
