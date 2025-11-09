# browser_chrome_tabs_api.py
import socket, ssl, sys, urllib.parse, tkinter, tkinter.font
import threading  # for timers and task scheduling
import ctypes  # needed for Skia/SDL pointer handling
import time
import email.utils

# Prefer Skia/SDL if available
try:
    import sdl2, skia
    SKIA_OK = True
except Exception:
    SKIA_OK = False

# Tk rendering active? (used by get_font)
TK_ACTIVE = True

# Choose a non-deprecated default typeface for Skia fonts.
# When SKIA_OK is True, pick a platform-appropriate family instead of
# relying on the deprecated default font. If Skia isn't available,
# leave this as None (and fall back to default behavior).
_SKIA_TYPEFACE = None
if 'skia' in globals() and SKIA_OK:
    try:
        import sys as _sys
        _families = []
        if _sys.platform == "darwin":
            _families = ["Helvetica", "Arial", "Menlo", "Courier"]
        elif _sys.platform.startswith("win"):
            _families = ["Arial", "Segoe UI", "Calibri", "Courier New"]
        else:
            _families = ["DejaVu Sans", "Liberation Sans", "Arial"]
        _tf = None
        for _fam in _families:
            try:
                _tf = skia.Typeface.MakeFromName(_fam, skia.FontStyle.Normal())
            except Exception:
                _tf = None
            if _tf:
                break
        if not _tf:
            # ensure fallback to a widely available family
            try:
                _tf = skia.Typeface.MakeFromName("DejaVu Sans", skia.FontStyle.Normal())
            except Exception:
                _tf = None
        _SKIA_TYPEFACE = _tf
    except Exception:
        _SKIA_TYPEFACE = None

# Internal helper to construct a Skia Font with a non-deprecated typeface.
# Always prefer _SKIA_TYPEFACE, and attempt a common fallback before
# ultimately falling back to the deprecated default. This avoids warnings
# about using the "Default font" in newer versions of Skia.
def _get_skia_font(pt_size: int) -> 'skia.Font':
    """Return a Skia Font object for the given point size.

    This helper tries to use the globally selected `_SKIA_TYPEFACE`. If that
    isn't available, it attempts to load a common typeface (e.g., DejaVu Sans)
    appropriate for most platforms. If that also fails, it falls back to
    passing None, which may trigger a deprecation warning but still returns
    a functional font.
    """
    # Import skia lazily to avoid errors when SKIA_OK is False.
    try:
        import skia
    except Exception:
        return None  # type: ignore
    tf = _SKIA_TYPEFACE
    if tf is None:
        # Try a common fallback family
        try:
            tf = skia.Typeface.MakeFromName("DejaVu Sans", skia.FontStyle.Normal())
        except Exception:
            tf = None
    try:
        # When tf is None, Skia will fall back to its default typeface.
        return skia.Font(tf, pt_size)
    except Exception:
        # Final fallback
        return skia.Font(None, pt_size)

# Frame refresh rate constant. Used to schedule animation frames. The
# browser will attempt to schedule a render roughly once every
# REFRESH_RATE_SEC seconds when an animation frame is requested. This
# corresponds to ~30 frames per second. See Chapter 12 for details.
REFRESH_RATE_SEC: float = 0.033


# Optional dependency for JavaScript execution. DukPy wraps the Duktape
# JavaScript engine. If it isn't available, interactive scripts will
# not run. Exercises in Chapter 9 rely on this module.
try:
    import dukpy  # type: ignore
except Exception:
    dukpy = None

# ----------------------- Tasks and Scheduling -----------------------
class Task:
    """
    A simple task abstraction. Stores a callable and its arguments and
    can be run at a later time. After running, it drops references to
    assist garbage collection.
    """
    def __init__(self, task_code, *args):
        self.task_code = task_code
        self.args = args

    def run(self):
        try:
            # Unpack arguments and invoke the callable
            if self.task_code:
                self.task_code(*self.args)
        finally:
            # Clear references
            self.task_code = None
            self.args = None


class TaskRunner:
    """
    A simple FIFO task runner. Maintains a queue of tasks to run and
    exposes methods to schedule and run tasks. A threading.Condition
    guards access to the queue so that tasks can be enqueued from
    multiple threads (e.g., timer threads) safely. The run() method
    removes and runs one task if available.
    """
    def __init__(self, tab):
        self.tab = tab
        self.tasks = []
        self.condition = threading.Condition()

    def schedule_task(self, task: Task) -> None:
        """Add a task to the queue in a thread-safe way."""
        with self.condition:
            self.tasks.append(task)
            # Wake up any waiting threads
            self.condition.notify_all()

    def run(self) -> None:
        """Run the next task from the queue, if there is one."""
        task = None
        with self.condition:
            if self.tasks:
                task = self.tasks.pop(0)
        if task:
            try:
                task.run()
            except Exception:
                # Ignore exceptions from tasks to keep the event loop running
                pass


# JavaScript helper to run a setTimeout callback by handle. Passed to
# interp.evaljs with dukpy.handle specifying which callback to run.
SETTIMEOUT_JS = "__runSetTimeout(dukpy.handle)"

# JavaScript helper to run an XMLHttpRequest onload callback by handle. This
# invokes the __runXHROnload function in JS, passing the response body and
# the request's handle. The dukpy.out parameter contains the response
# string and dukpy.handle contains the XHR object's handle. See
# JSContext.dispatch_xhr_onload for details.
XHR_ONLOAD_JS = "__runXHROnload(dukpy.out, dukpy.handle)"

# ================= Cookies =================
# COOKIE_JAR stores cookies per origin. Each origin maps to a dict
# mapping cookie names to (value, params) tuples. The params dict
# contains cookie attributes like 'httponly', 'expires', etc. Cookies
# are set via Set-Cookie headers and sent back on subsequent
# requests via the Cookie header.
COOKIE_JAR: dict[str, dict[str, tuple[str, dict[str, str]]]] = {}

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

    def origin(self) -> str:
        """Return the origin (scheme + host + port) of this URL."""
        return f"{self.scheme}://{self.host}:{self.port}"

    def request(self, referrer: str | None = None, payload: str | None = None, origin: str | None = None):
        """
        Make an HTTP request to this URL. Sends cookies from the COOKIE_JAR
        for this origin and stores any Set-Cookie headers. Includes
        optional Referer and Origin headers. Returns a tuple of
        (headers, body).
        """
        # Connect to server
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        s.connect((self.host, self.port))
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            try:
                s = ctx.wrap_socket(s, server_hostname=self.host)
            except ssl.SSLError:
                # Certificate errors will propagate to caller
                s.close()
                raise
        # Build request
        method = "POST" if payload is not None else "GET"
        req = f"{method} {self.path} HTTP/1.0\r\nHost: {self.host}\r\n"
        # Referer header
        if referrer:
            req += f"Referer: {referrer}\r\n"
        # Origin header
        if origin:
            req += f"Origin: {origin}\r\n"
        # Cookie header
        jar_key = self.origin()
        cookies: list[str] = []
        now = time.time()
        jar = COOKIE_JAR.get(jar_key, {})
        # Determine request method and cross-site conditions for SameSite
        method = "POST" if payload is not None else "GET"
        ref_origin = None
        if referrer:
            try:
                ref_origin = URL(referrer).origin()
            except Exception:
                ref_origin = None
        cross_site = ref_origin is not None and ref_origin != jar_key
        # Iterate through cookies and collect those that should be sent
        remove_names: list[str] = []
        for name, (value, params) in jar.items():
            # Expiration: skip and remove expired cookies
            exp = params.get('expires')
            if exp:
                try:
                    # If stored as timestamp
                    expires_ts = float(exp) if not isinstance(exp, (str, bytes)) else float(exp)
                except Exception:
                    try:
                        dt = email.utils.parsedate_to_datetime(str(exp))
                        expires_ts = dt.timestamp()
                    except Exception:
                        expires_ts = None
                if expires_ts is not None and now > expires_ts:
                    remove_names.append(name)
                    continue
            # SameSite=Lax: skip on cross-site POST
            same_site = params.get('samesite', '').lower()
            if same_site == 'lax' and method == 'POST' and cross_site:
                continue
            cookies.append(f"{name}={value}")
        # Remove expired cookies
        for n in remove_names:
            jar.pop(n, None)
        if cookies:
            req += f"Cookie: {'; '.join(cookies)}\r\n"
        # Content headers
        if payload is not None:
            length = len(payload.encode("utf8"))
            req += "Content-Type: application/x-www-form-urlencoded\r\n"
            req += f"Content-Length: {length}\r\n"
        req += "\r\n"
        if payload is not None:
            req += payload
        # Send request
        s.send(req.encode("utf8"))
        # Read response
        resp = s.makefile("r", encoding="utf8", newline="\r\n")
        # Status line
        _ = resp.readline()
        headers: dict[str, str] = {}
        while True:
            line = resp.readline()
            if line == "\r\n" or line == "":
                break
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k_lower = k.casefold()
            v = v.strip()
            # Collect duplicate headers (e.g., Set-Cookie)
            if k_lower in headers and k_lower == "set-cookie":
                headers[k_lower] += ", " + v
            else:
                headers[k_lower] = v
        # No transfer-encoding or content-encoding expected
        body = resp.read()
        s.close()
        # Store cookies from Set-Cookie header
        sc = headers.get("set-cookie")
        if sc:
            # There may be multiple cookies separated by comma
            cookie_headers = [x.strip() for x in sc.split(",")]
            for cookie_str in cookie_headers:
                if not cookie_str:
                    continue
                parts = [p.strip() for p in cookie_str.split(";")]
                if not parts:
                    continue
                name_value = parts[0]
                if "=" not in name_value:
                    continue
                name, val = name_value.split("=", 1)
                params: dict[str, str] = {}
                for part in parts[1:]:
                    if not part:
                        continue
                    if "=" in part:
                        k_p, v_p = part.split("=", 1)
                        key = k_p.casefold()
                        params[key] = v_p
                    else:
                        key = part.casefold()
                        params[key] = ""
                # Convert Expires to timestamp if possible
                if 'expires' in params:
                    exp_val = params['expires']
                    try:
                        # Try to parse to timestamp
                        dt = email.utils.parsedate_to_datetime(str(exp_val))
                        params['expires'] = dt.timestamp()
                    except Exception:
                        pass
                # Save cookie
                COOKIE_JAR.setdefault(jar_key, {})[name] = (val, params)
        return headers, body

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
    # Skia path: return a Tk-compatible shim (no deprecated default font)
    try:
        if not TK_ACTIVE:
            import sys, skia
            # pick a real family by OS
            if sys.platform == "darwin":
                families = ["Helvetica", "Arial", "Menlo", "Courier"]
            elif sys.platform.startswith("win"):
                families = ["Arial", "Segoe UI", "Calibri", "Courier New"]
            else:
                families = ["DejaVu Sans", "Liberation Sans", "Arial"]

            tf = None
            for fam in families:
                tf = skia.Typeface.MakeFromName(fam, skia.FontStyle.Normal())
                if tf: break
            if not tf:
                tf = skia.Typeface.MakeFromName("DejaVu Sans", skia.FontStyle.Normal())

            class _SkiaFontShim:
                def __init__(self, tf, size):
                    self.size = float(size)
                    self._font = skia.Font(tf, self.size)

                def measure(self, text: str) -> float:
                    return float(self._font.measureText(text))

                # Tk-compatible:
                #   metrics() -> dict
                #   metrics("ascent") -> number
                def metrics(self, key=None):
                    m = self._font.getMetrics()
                    ascent  = float(-m.fAscent)  # Skia ascent is negative; Tk expects +ve
                    descent = float(m.fDescent)
                    leading = float(getattr(m, "fLeading", 0.0))
                    data = {
                        "ascent": ascent,
                        "descent": descent,
                        "linespace": ascent + descent + leading,
                    }
                    if isinstance(key, str):
                        return data.get(key)
                    return data

            return _SkiaFontShim(tf, size)
    except NameError:
        # TK_ACTIVE not defined yet; fall through to Tk path
        pass

    # Tk path (unchanged)
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

    # Inline style parsing (chapter 11)
    if isinstance(node, Element) and "style" in getattr(node, 'attributes', {}):
        for decl in node.attributes.get("style",""
                    ).split(";"):
            if ":" in decl:
                k,v = decl.split(":",1)
                node.style[k.strip().casefold()] = v.strip()

    # Defaults for chapter 11 visual effects
    node.style.setdefault("background-color", "transparent")
    node.style.setdefault("border-radius", "0px")
    if node.style["font-size"].endswith("%"):
        parent_px = float((node.parent.style["font-size"] if node.parent else INHERITED_PROPERTIES["font-size"])[:-2])
        node_pct = float(node.style["font-size"][:-1]) / 100
        node.style["font-size"] = str(node_pct * parent_px) + "px"
    for c in node.children:
        style(c, rules)


def _px_from_length(value: str, x1, y1, x2, y2):
    """
    Convert a CSS length value to pixels.
    Supports 'px' and '%' units. For unsupported units, raises NotImplementedError.
    """
    try:
        v = value.strip()
        if v.endswith("px"):
            return float(v[:-2])
        elif v.endswith("%"):
            w = abs(x2 - x1)
            h = abs(y2 - y1)
            return (w + h) * 0.5 * float(v[:-1]) / 100.0
        elif v == "" or v == "0":
            return 0.0
        else:
            # Try to parse as float
            return float(v)
    except Exception:
        raise NotImplementedError(f"Unsupported length value: {value}")

def _parse_color(c: str):
    NAMED = {
        "black": 0xFF000000, "white": 0xFFFFFFFF, "gray": 0xFF808080,
        "lightblue": 0xFFADD8E6, "orange": 0xFFFFA500, "red": 0xFFFF0000,
        "blue": 0xFF0000FF, "green": 0xFF008000, "lightgray": 0xFFD3D3D3
    }
    c = (c or "").strip().lower()
    if c in NAMED: return NAMED[c]
    if c.startswith("#"):
        h = c[1:]
        if len(h) == 3:
            r = int(h[0]*2, 16); g = int(h[1]*2, 16); b = int(h[2]*2, 16)
        else:
            r = int(h[0:2], 16); g = int(h[2:4], 16); b = int(h[4:6], 16)
        return (0xFF<<24) | (r<<16) | (g<<8) | b
    return 0xFF000000

    try:
        v = value.strip()
        if v.endswith("px"): return float(v[:-2])
        if v.endswith("%"):
            w = abs(x2 - x1); h = abs(y2 - y1)
            return (w + h) * 0.5 * float(v[:-1]) / 100.0
        return float(v)
    except Exception:
        return 0.0
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
        # Determine input type; treat default as text
        itype = node.attributes.get("type", "text").lower()
        # Exercise 10-1: Hidden inputs do not take up space or draw anything
        if itype == "hidden":
            return
        # compute font used inside widget
        weight = node.style["font-weight"]
        style = node.style["font-style"]
        if style == "normal": style = "roman"
        size = int(float(node.style["font-size"][:-2]) * .75)
        font = get_font(size, weight, style)

        # size by type
        is_checkbox = itype == "checkbox"
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
                br = node.style.get("border-radius", "0px")
                radius = _px_from_length(br, *rect)
                self.display_list.append(("rrect", rect, bgcolor, radius))

            # Determine displayed text for input or button
            if node.tag == "input":
                raw = node.attributes.get("value", "")
                # Password inputs show stars instead of characters
                if itype == "password":
                    text = "".join("•" for _ in raw)
                else:
                    text = raw
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

    # Chapter 7+ API compatibility: new_line and self_rect
    def new_line(self) -> None:
        """
        Start a new line of inline content. In this implementation, a new line
        is equivalent to flushing the current line. Provided for API
        compatibility with later chapters.
        """
        self.flush()

    def self_rect(self) -> 'Rect':
        """
        Return the bounding rectangle of this block. Provided for API
        compatibility with later chapters. Note that width and height may not
        be set until layout is called.
        """
        try:
            right = self.x + (self.width or 0)
            bottom = self.y + (self.height or 0)
        except Exception:
            right = self.x
            bottom = self.y
        return Rect(self.x, self.y, right, bottom)

    def should_paint(self):
        if isinstance(self.node, Element) and self.node.tag in ["input","button"]:
            return False
        return True

    def paint(self):
        cmds = []
        # background for element blocks per chapter 11
        if isinstance(self.node, Element):
            bgcolor = self.node.style.get("background-color", "transparent")
            if bgcolor != "transparent":
                x1,y1 = self.x, self.y
                x2,y2 = self.x + (self.width or 0), self.y + (self.height or 0)
                br = self.node.style.get("border-radius", "0px")
                radius = _px_from_length(br, x1,y1,x2,y2)
                cmds.append(DrawRRect(x1, y1, x2, y2, bgcolor, radius))
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
            elif item[0] == "rrect":
                _, (x1,y1,x2,y2), color, radius = item
                cmds.append(DrawRRect(x1, y1, x2, y2, color, radius))
            elif item[0] == "line":
                _, (x1,y1,x2,y2,color,th) = item
                cmds.append(DrawLine(x1, y1, x2, y2, color, th))
            elif item[0] == "outline":
                _, (x1,y1,x2,y2), color, th = item
                cmds.append(DrawOutline(x1, y1, x2, y2, color, th))
        return cmds

# ================= Inline layout classes (Chapter 7+ compatibility) =================
# The following classes are provided to match the API expected after Chapter 7. In
# this simplified browser implementation, inline words and widgets are managed
# directly by BlockLayout using a display list. These classes are therefore
# minimal stubs: they store child nodes and compute sizes, but do not change
# the rendering behaviour. They ensure that the names used in the book’s
# outlines exist so that scripts referring to them do not crash.

class LineLayout:
    """
    Represents a single line of inline content. Each line lays out its
    children horizontally and computes a baseline so that text aligns
    properly. This implementation follows the Web Browser Engineering
    reference design: it spans the full width of its parent block, stacks
    vertically below the previous line, and positions its child layout
    objects (TextLayout or InputLayout) accordingly.
    """
    def __init__(self, node, parent, previous):
        self.node = node
        self.parent = parent
        self.previous = previous
        self.children: list[object] = []
        self.x = 0
        self.y = 0
        self.width = 0
        self.height = 0

    def layout(self) -> None:
        # Lines span the full width of the parent block
        self.width = getattr(self.parent, 'width', WIDTH)
        self.x = getattr(self.parent, 'x', 0)
        # Stack vertically below the previous line
        if self.previous:
            self.y = self.previous.y + self.previous.height
        else:
            self.y = getattr(self.parent, 'y', 0)
        # Lay out children (TextLayout or InputLayout)
        for child in self.children:
            child.layout()
        if not self.children:
            self.height = 0
            return
        # Compute baseline from maximum ascents/descents of children
        max_ascent = max(child.font.metrics("ascent") for child in self.children)
        max_descent = max(child.font.metrics("descent") for child in self.children)
        baseline = self.y + max_ascent
        # Position children relative to baseline
        for child in self.children:
            child.y = baseline - child.font.metrics("ascent")
        # Height: leave extra space for descenders and line spacing
        self.height = 1.25 * (max_ascent + max_descent)

    def paint(self) -> list:
        # Lines themselves draw nothing; children handle painting
        return []

    def should_paint(self) -> bool:
        return True

class TextLayout:
    """
    A single word within a line. It computes its font from CSS styles,
    measures its width and height, and positions itself next to the
    previous inline object (with a space) or at the start of the line.
    """
    def __init__(self, node, word, parent, previous):
        self.node = node
        self.word = word
        self.parent = parent
        self.previous = previous
        self.children: list = []
        self.x = 0
        self.y = 0
        self.width = 0
        self.height = 0
        self.font = None

    def layout(self) -> None:
        # Compute the font from inherited styles
        weight = self.node.style.get("font-weight", "normal")
        style = self.node.style.get("font-style", "normal")
        if style == "normal":
            style = "roman"
        # Default font size is 16px; convert to points (approx 0.75)
        size_str = self.node.style.get("font-size", "16px")
        try:
            px = float(size_str[:-2])
        except Exception:
            px = 16.0
        size = int(px * 0.75)
        self.font = get_font(size, weight, style)
        self.width = self.font.measure(self.word)
        # Place after previous word with a space
        if self.previous:
            space = self.previous.font.measure(" ")
            self.x = self.previous.x + self.previous.width + space
        else:
            self.x = getattr(self.parent, 'x', 0)
        self.height = self.font.metrics("linespace")

        # If this text node is an anchor (<a href="...">), register its
        # bounding box for click detection. The rect covers the full
        # width and line height of the text. This enables link clicks in
        # Skia mode when the user clicks on anchor text.
        try:
            if isinstance(self.node, Element) and self.node.tag == "a":
                # Only register if the anchor has an href attribute
                if "href" in getattr(self.node, "attributes", {}):
                    x1 = self.x
                    y1 = self.y
                    x2 = self.x + self.width
                    y2 = self.y + self.height
                    Browser._register_widget_box(self.node, (x1, y1, x2, y2))
        except Exception:
            pass

    def paint(self) -> list:
        color = self.node.style.get("color", "black")
        return [DrawText(self.x, self.y, self.word, self.font, color)]

    def should_paint(self) -> bool:
        return True

class InputLayout:
    """
    Layout object for <input> and <button> elements.  Draws a coloured
    rectangle and the element’s value or label. This implementation
    follows the Web Browser Engineering design and is extended to
    support hidden inputs and password masking as described in
    Chapter 10 exercises.
    """
    def __init__(self, node, parent, previous):
        self.node = node
        self.parent = parent
        self.previous = previous
        self.children: list = []
        self.x = 0
        self.y = 0
        self.width = 0
        self.height = 0
        self.font = None

    def layout(self) -> None:
        # Font for input or button
        weight = self.node.style.get("font-weight", "normal")
        style = self.node.style.get("font-style", "normal")
        if style == "normal":
            style = "roman"
        size_str = self.node.style.get("font-size", "16px")
        try:
            px = float(size_str[:-2])
        except Exception:
            px = 16.0
        size = int(px * 0.75)
        self.font = get_font(size, weight, style)
        # Determine the type of input (text, hidden, password, checkbox, etc.)
        itype = self.node.attributes.get("type", "text").lower() if self.node.tag == "input" else None
        # Width: hidden inputs have no width or height
        if itype == "hidden":
            self.width = 0
            self.height = 0
            return
        if self.node.tag == "input":
            # Fixed width for text inputs, password, etc.
            self.width = INPUT_WIDTH_PX
        else:
            # Button width based on its label
            text = ""
            if len(self.node.children) == 1 and isinstance(self.node.children[0], Text):
                text = self.node.children[0].text
            self.width = max(80, self.font.measure(text) + 20)
        # Horizontal position: after previous inline object with a space
        if self.previous:
            space = self.previous.font.measure(" ")
            self.x = self.previous.x + self.previous.width + space
        else:
            self.x = getattr(self.parent, 'x', 0)
        # Height based on font
        self.height = self.font.metrics("linespace")

    def should_paint(self) -> bool:
        # Hidden inputs take no space and are not painted
        if self.node.tag == "input":
            itype = self.node.attributes.get("type", "text").lower()
            return itype != "hidden"
        return True

    def paint(self) -> list:
        cmds: list = []
        # If layout hasn't been run yet, compute it
        if self.font is None:
            self.layout()
        # Hidden inputs draw nothing
        if self.node.tag == "input":
            itype = self.node.attributes.get("type", "text").lower()
            if itype == "hidden":
                return cmds
        # Draw background rectangle if specified
        bgcolor = self.node.style.get("background-color", "transparent")
        if bgcolor != "transparent":
            rect = self.self_rect()
            cmds.append(DrawRect(rect.left, rect.top, rect.right, rect.bottom, bgcolor))
        # Determine text value to draw
        text_value = ""
        if self.node.tag == "input":
            raw = self.node.attributes.get("value", "")
            itype = self.node.attributes.get("type", "text").lower()
            if itype == "password":
                text_value = "".join("•" for _ in raw)
            else:
                text_value = raw
        else:
            if len(self.node.children) == 1 and isinstance(self.node.children[0], Text):
                text_value = self.node.children[0].text
            else:
                text_value = ""
        # Font and colour
        color = self.node.style.get("color", "black")
        cmds.append(DrawText(self.x, self.y, text_value, self.font, color))
        return cmds

    def self_rect(self) -> 'Rect':
        return Rect(self.x, self.y, self.x + self.width, self.y + self.height)

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
// Document.cookie API: forwards to Python get_cookie/set_cookie
Object.defineProperty(document, "cookie", {
  get: function() {
    return call_python("get_cookie");
  },
  set: function(value) {
    call_python("set_cookie", value.toString());
  }
});
// Minimal XMLHttpRequest implementation (supports sync and async)
// Each XHR object stores a unique handle in XHR_REQUESTS so Python
// can identify which JS object to call back into. The `open`
// method stores the HTTP method, URL and whether the request is
// asynchronous. The `send` method calls into Python with the extra
// arguments `is_async` and `handle`. In the synchronous case,
// send() returns the response body which is saved to `responseText`.
var XHR_REQUESTS = {};
function XMLHttpRequest() {
  this.handle = Object.keys(XHR_REQUESTS).length;
  XHR_REQUESTS[this.handle] = this;
  this.is_async = false;
  this.method = null;
  this.url = null;
}
XMLHttpRequest.prototype.open = function(method, url, is_async) {
  this.is_async = !!is_async;
  this.method = method;
  this.url = url;
};
XMLHttpRequest.prototype.send = function(body) {
  // Body may be null or a string. In the async case, Python will
  // start the request on a new thread and return immediately. In
  // the sync case, it returns the response body, which we store.
  var result = call_python("XMLHttpRequest_send",
      this.method,
      this.url.toString(),
      body,
      this.is_async,
      this.handle);
  if (!this.is_async) {
    this.responseText = result;
  }
};
// Called by Python when an async XHR completes. It finds the JS
// XHR object by handle and calls its onload handler, if present,
// passing in a 'load' event. It also sets responseText.
function __runXHROnload(body, handle) {
  var obj = XHR_REQUESTS[handle];
  var evt = new Event('load');
  obj.responseText = body;
  if (obj.onload) {
    obj.onload(evt);
  }
}

// ---------------- Task-based timers and animation frames ----------------
// Storage for pending setTimeout callbacks. Each entry maps a handle
// to a callback function. The handle is just the number of callbacks
// seen so far.
var SET_TIMEOUT_REQUESTS = {};

// setTimeout schedules a callback to run after a given number of
// milliseconds. It stores the callback in SET_TIMEOUT_REQUESTS and
// notifies Python to create a timer. Returns a handle, but our
// implementation does not support clearTimeout.
function setTimeout(callback, time_delta) {
  var handle = Object.keys(SET_TIMEOUT_REQUESTS).length;
  SET_TIMEOUT_REQUESTS[handle] = callback;
  call_python("setTimeout", handle, time_delta);
  return handle;
}

// __runSetTimeout is called by Python when a timer fires. It finds the
// callback by handle and runs it. Note that we do not remove the
// callback from SET_TIMEOUT_REQUESTS; this is a leak, but acceptable
// for our toy browser.
function __runSetTimeout(handle) {
  var callback = SET_TIMEOUT_REQUESTS[handle];
  if (callback) callback();
}

// Storage for requestAnimationFrame listeners. Each frame we run
// the accumulated callbacks and reset the list. Any callbacks
// registered during a frame are scheduled for the next frame.
var RAF_LISTENERS = [];

// requestAnimationFrame registers a function to run before the next
// render. It stores the function and notifies Python to schedule
// the render. Returns nothing.
function requestAnimationFrame(fn) {
  RAF_LISTENERS.push(fn);
  call_python("requestAnimationFrame");
}

// __runRAFHandlers runs all listeners registered for the current
// frame. It resets RAF_LISTENERS so that any new listeners are for
// the next frame.
function __runRAFHandlers() {
  var handlers_copy = RAF_LISTENERS;
  RAF_LISTENERS = [];
  for (var i = 0; i < handlers_copy.length; i++) {
    handlers_copy[i]();
  }
}

// Node.style setter: forwards style strings to Python. Setting
// style triggers a re-render via set_needs_render on the Python side.
Object.defineProperty(Node.prototype, 'style', {
  set: function(s) {
    call_python("style_set", this.handle, s.toString());
  }
});
"""

# When dispatching an event from Python, we call this snippet. It
# constructs a new Event and dispatches it on a Node, returning
# true if the default action should run and false if it should be
# prevented. The Python side will invert this to determine whether
# to skip the default action.
EVENT_DISPATCH_JS = "new Node(dukpy.handle).dispatchEvent(new Event(dukpy.type))"


class DrawRRect:
    def __init__(self, x1, y1, x2, y2, color, radius):
        self.top = y1; self.left = x1
        self.bottom = y2; self.right = x2
        self.color = color; self.radius = max(0.0, float(radius))
    def execute(self, scroll, canvas):
        x1,y1,x2,y2 = self.left, self.top - scroll, self.right, self.bottom - scroll
        r = max(0.0, min(self.radius, min((x2-x1)/2, (y2-y1)/2)))
        if r <= 0:
            canvas.create_rectangle(x1,y1,x2,y2, width=0, fill=self.color); return
        # center rects
        canvas.create_rectangle(x1+r, y1, x2-r, y2, width=0, fill=self.color)
        canvas.create_rectangle(x1, y1+r, x1+r, y2-r, width=0, fill=self.color)
        canvas.create_rectangle(x2-r, y1+r, x2, y2-r, width=0, fill=self.color)
        # arcs
        canvas.create_arc(x1, y1, x1+2*r, y1+2*r, start=90, extent=90, outline=self.color, fill=self.color)
        canvas.create_arc(x2-2*r, y1, x2, y1+2*r, start=0,  extent=90, outline=self.color, fill=self.color)
        canvas.create_arc(x2-2*r, y2-2*r, x2, y2, start=270, extent=90, outline=self.color, fill=self.color)
        canvas.create_arc(x1, y2-2*r, x1+2*r, y2, start=180, extent=90, outline=self.color, fill=self.color)
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
        # Cookie API: reading/writing document.cookie
        self.interp.export_function("get_cookie", self.get_cookie)
        self.interp.export_function("set_cookie", self.set_cookie)
        # XMLHttpRequest support
        self.interp.export_function("XMLHttpRequest_send", self.XMLHttpRequest_send)
        # Timers and animation frames support
        self.interp.export_function("style_set", self.style_set)
        self.interp.export_function("setTimeout", self.setTimeout)
        self.interp.export_function("requestAnimationFrame", self.requestAnimationFrame)
        # Whether this context has been discarded (e.g., page navigated away)
        self.discarded = False
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

    def run(self, script: str, code: str | None = None) -> None:
        """
        Execute JavaScript code in this context. For compatibility with the
        book’s outline, this method accepts two parameters (script and code).
        When both are provided, the second parameter takes precedence; when
        only one is provided, it is treated as the code to execute. Any
        exceptions from the JS interpreter are printed but otherwise ignored
        to avoid crashing the browser.
        """
        # Determine which argument contains the actual code
        js_code = code if code is not None else script
        try:
            self.interp.evaljs(js_code)
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

    # ----- XMLHttpRequest API -----
    def XMLHttpRequest_send(self, method: str, url: str,
                            body: str | None = None,
                            isasync: bool = False,
                            handle: int | None = None):
        """
        Handle an XMLHttpRequest from JavaScript. Supports both synchronous
        and asynchronous requests. The `isasync` flag controls whether the
        request should block JavaScript execution. In the synchronous case,
        the response body is returned directly. In the asynchronous case,
        a Python thread performs the request and enqueues a task to call
        __runXHROnload on the JavaScript side when the response arrives.

        The `handle` identifies the XHR object on the JS side; it must
        be passed back to __runXHROnload so that the correct object's
        onload callback and responseText property are set.
        """
        # Resolve the URL relative to the current tab's URL
        try:
            full_url = self.tab.url.resolve(url)
        except Exception as ex:
            raise Exception(f"Invalid XHR URL: {ex}")
        # Check Content-Security-Policy
        if not self.tab.allowed_request(full_url):
            raise Exception("Cross-origin XHR blocked by CSP")

        # Helper to perform the request and schedule the JS callback
        def run_load() -> str:
            # Perform the request; include referer and origin headers
            try:
                ref = str(self.tab.url) if self.tab.url else None
                origin = self.tab.url.origin() if self.tab.url else None
                headers, out = full_url.request(referrer=ref, payload=body, origin=origin)
            except Exception as ex:
                # Propagate network errors to JavaScript by re-raising
                raise Exception(str(ex))
            # Enforce same-origin policy unless CORS allows
            req_origin = self.tab.url.origin() if self.tab.url else None
            resp_origin = full_url.origin()
            if req_origin is not None and resp_origin != req_origin:
                # Look for Access-Control-Allow-Origin header
                allow = None
                for k, v in (headers or {}).items():
                    if k.lower() == "access-control-allow-origin":
                        allow = v.strip()
                        break
                if not allow or (allow != "*" and allow != req_origin):
                    raise Exception("Cross-origin XHR request not allowed")
            # If this is an asynchronous XHR, schedule the onload callback
            if isasync and handle is not None:
                try:
                    # Create a task to call dispatch_xhr_onload. Note that
                    # out may be bytes or str; we ensure a str is passed.
                    resp_str = out.decode('utf-8', errors='ignore') if isinstance(out, bytes) else out
                    task = Task(self.dispatch_xhr_onload, resp_str, handle)
                    self.tab.task_runner.schedule_task(task)
                except Exception:
                    pass
            return out

        # Synchronous requests block until the response arrives and then
        # return the body. Asynchronous requests run in a separate
        # thread; the result will be delivered via dispatch_xhr_onload.
        try:
            if not isasync:
                return run_load()
            else:
                threading.Thread(target=run_load).start()
                # Return an empty string to JS for async requests; the
                # onload callback will set responseText when ready
                return ""
        except Exception as ex:
            # Raise exceptions so that JS sees an error
            raise Exception(str(ex))

    # ----- XHR onload dispatch -----
    def dispatch_xhr_onload(self, body: str, handle: int) -> None:
        """
        Called when an asynchronous XMLHttpRequest completes. If this
        context has not been discarded, this method invokes the
        __runXHROnload JavaScript helper with the response body and
        handle, which in turn calls the onload callback on the JS
        XMLHttpRequest object. If the context has been discarded
        (because the page navigated away), the callback is skipped.
        """
        # If we've navigated away, ignore callbacks from old pages
        if getattr(self, 'discarded', False):
            return
        try:
            # Evaluate the JS helper to invoke the callback
            self.interp.evaljs(XHR_ONLOAD_JS, out=body, handle=handle)
        except Exception:
            pass

    # ----- Cookie API for document.cookie -----
    def get_cookie(self) -> str:
        """
        Return a string representation of cookies for the current tab's origin.
        Includes cookie parameters (e.g., Expires, HttpOnly, SameSite) using the
        same format as the Cookie header, separated by '; '. Cookies marked
        HttpOnly are not returned to JavaScript. Expired cookies are removed.
        """
        try:
            origin = self.tab.url.origin()
        except Exception:
            return ""
        now = time.time()
        cookies = []
        jar = COOKIE_JAR.get(origin, {})
        # Remove expired cookies and build the cookie string
        expired = []
        for name, (val, params) in jar.items():
            # Skip HttpOnly cookies when reading
            if any(k.lower() == 'httponly' for k in params):
                continue
            # Check expiration
            exp = params.get('expires')
            if exp:
                try:
                    # If expiration stored as timestamp
                    if isinstance(exp, (int, float)):
                        expires_ts = float(exp)
                    else:
                        # Parse RFC 1123 date string
                        dt = email.utils.parsedate_to_datetime(str(exp))
                        expires_ts = dt.timestamp()
                except Exception:
                    expires_ts = None
                if expires_ts is not None and now > expires_ts:
                    expired.append(name)
                    continue
            # Build cookie string: name=value plus parameters
            parts = [f"{name}={val}"]
            for k, v in params.items():
                # Skip internal parameters that are empty for HttpOnly (already skipped)
                if k.lower() == 'httponly':
                    continue
                if v == "":
                    parts.append(k)
                else:
                    parts.append(f"{k}={v}")
            cookies.append("; ".join(parts))
        # Remove expired cookies from jar
        for name in expired:
            jar.pop(name, None)
        return "; ".join(cookies)

    def set_cookie(self, cookie_str: str) -> None:
        """
        Set a cookie for the current tab's origin. The cookie string should be
        formatted like 'name=value; Expires=...; HttpOnly; SameSite=Lax'. The
        HttpOnly attribute prevents modification from JavaScript. The Expires
        attribute (RFC 1123 date) sets expiration. SameSite=Lax cookies will
        be sent on same-site requests and cross-site GET requests but not on
        cross-site POSTs. Secure and other attributes are stored but not used.
        """
        try:
            origin = self.tab.url.origin()
        except Exception:
            return
        cookie_str = str(cookie_str).strip()
        if not cookie_str:
            return
        # Split on ';' to parse name=value and parameters
        parts = [p.strip() for p in cookie_str.split(';')]
        if not parts:
            return
        first = parts[0]
        if '=' not in first:
            return
        name, val = first.split('=', 1)
        params: dict[str, str] = {}
        for part in parts[1:]:
            if not part:
                continue
            if '=' in part:
                k, v = part.split('=', 1)
                params[k.casefold()] = v
            else:
                params[part.casefold()] = ""
        # If attempting to set an HttpOnly cookie via JS, ignore
        if any(k.lower() == 'httponly' for k in params):
            return
        # Parse Expires into timestamp if provided
        exp = params.get('expires')
        if exp:
            try:
                dt = email.utils.parsedate_to_datetime(str(exp))
                params['expires'] = dt.timestamp()
            except Exception:
                # Leave as string if parsing fails
                pass
        # Store in cookie jar
        COOKIE_JAR.setdefault(origin, {})[name] = (val, params)

    # ----- style attribute support -----
    def style_set(self, handle: int, s: str) -> None:
        """
        Set the style attribute on a node and mark the tab as needing
        re-rendering. Called from JavaScript via call_python when
        setting Node.style = ...
        """
        node = self.handle_to_node.get(handle)
        if isinstance(node, Element):
            # Update the style attribute (inline style)
            node.attributes["style"] = s
            # Notify the tab that rendering is needed
            if hasattr(self.tab, "set_needs_render"):
                self.tab.set_needs_render()

    # ----- setTimeout support -----
    def dispatch_settimeout(self, handle: int) -> None:
        """
        Called when a timer fires. Runs the JavaScript callback
        associated with the given handle, unless this JS context has
        been discarded (e.g., due to navigation).
        """
        if getattr(self, 'discarded', False):
            return
        try:
            # Evaluate the callback stored in JS side
            self.interp.evaljs(SETTIMEOUT_JS, handle=handle)
        except Exception:
            pass

    def setTimeout(self, handle: int, time_ms: int) -> None:
        """
        Schedule a callback to run after time_ms milliseconds. This
        method is called from JavaScript via call_python. The timer
        callback will enqueue a task on the tab's task runner to run
        the JavaScript callback. Thread-safe.
        """
        delay = (time_ms or 0) / 1000.0

        def run_callback():
            # Create a Task to invoke the callback in the main loop
            task = Task(self.dispatch_settimeout, handle)
            try:
                self.tab.task_runner.schedule_task(task)
            except Exception:
                pass

        try:
            timer = threading.Timer(delay, run_callback)
            timer.start()
        except Exception:
            pass

    # ----- requestAnimationFrame support -----
    def requestAnimationFrame(self) -> None:
        """
        Called from JavaScript via call_python when requestAnimationFrame
        is invoked. Schedule a render task immediately on this tab's
        task runner. When the task runs, Tab.render will update the
        display list and then trigger the browser to draw. This
        simplified implementation ensures animations run smoothly
        without implementing a separate frame scheduler.
        """
        try:
            task = Task(self.tab.render)
            self.tab.task_runner.schedule_task(task)
        except Exception:
            pass

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
        self.allowed_origins: set[str] | None = None  # CSP allowed origins
        self.referrer_policy: str | None = None
        # Track certificate errors on last request. True if the last HTTPS
        # connection failed due to invalid certificate. Used to draw the lock
        # icon (or omit it) in the address bar.
        self.cert_error: bool = False
        # Initialize a task runner for this tab. Tasks are used to
        # schedule deferred JavaScript execution (e.g., script loading,
        # setTimeout callbacks, requestAnimationFrame) and run them
        # during the browser's event loop.
        self.task_runner = TaskRunner(self)
        # Dirty flag indicating whether render needs to recompute layout
        self.needs_render = False
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

    def set_needs_render(self) -> None:
        """
        Mark that this tab needs to be re-rendered. Setting this flag
        will cause the next call to render() to recompute layout and
        paint. Also notify the browser that it needs to redraw and
        schedule an animation frame, since a render implies a change
        visible on the next frame.
        """
        self.needs_render = True
        # Notify the browser that the chrome/tab needs to be redrawn
        try:
            if hasattr(self.browser, 'set_needs_raster_and_draw'):
                self.browser.set_needs_raster_and_draw()
        except Exception:
            pass
        # Request an animation frame on the browser for this tab
        try:
            if hasattr(self.browser, 'set_needs_animation_frame'):
                self.browser.set_needs_animation_frame(self)
        except Exception:
            pass

    def _restore_history_entry(self):
        entry = self.history[self.history_index]
        # 8-5 safety: never auto re-POST on history nav; do GET instead
        self.load(entry["url"], payload=None)

    def load(self, url, payload=None):
        # Determine referrer header per referrer policy on previous page
        referrer = None
        if self.history_index > 0 and self.history_index - 1 < len(self.history):
            prev = self.history[self.history_index - 1]
            prev_url = prev.get("url")
            if isinstance(prev_url, URL):
                # Check referrer policy on the previous page
                if self.referrer_policy == "no-referrer":
                    referrer = None
                elif self.referrer_policy == "same-origin":
                    if prev_url.origin() == url.origin():
                        referrer = str(prev_url)
                else:
                    referrer = str(prev_url)
        # Reset certificate error flag
        self.cert_error = False
        try:
            self.browser.set_status("Loading…")
            # Perform the network request, capturing headers and body
            headers, body = url.request(referrer=referrer, payload=payload)
            self.browser.set_status("")
        except ssl.SSLError:
            # Certificate error
            self.cert_error = True
            self.browser.set_status("⚠ Certificate error")
            # Update padlock icon after error
            try:
                self.browser.update_padlock()
            except Exception:
                pass
            return
        except Exception as ex:
            self.browser.set_status(f"Network error: {ex}")
            return
        self.url = url
        # Parse Content-Security-Policy for allowed origins
        self.allowed_origins = None
        csp = None
        for k, v in (headers or {}).items():
            if k.lower() == "content-security-policy":
                csp = v
                break
        if csp and "default-src" in csp:
            parts = csp.split()
            try:
                idx = parts.index("default-src")
                allowed = set()
                for item in parts[idx + 1:]:
                    item = item.strip().rstrip(";")
                    if item:
                        allowed.add(item)
                self.allowed_origins = allowed if allowed else None
            except ValueError:
                self.allowed_origins = None
        else:
            self.allowed_origins = None
        # Parse Referrer-Policy header
        rp = None
        for k, v in (headers or {}).items():
            if k.lower() == "referrer-policy":
                rp = v
                break
        self.referrer_policy = rp.strip().lower() if rp else None
        # Parse HTML document
        self.nodes = HTMLParser(body).parse()
        self.title = self._extract_title() or f"{url.host}"
        # Initialize JavaScript context
        if dukpy is not None:
            # Discard any existing JS context so that pending timers
            # don't call back into a context for an old page. Mark
            # discarded = True on the old context so callbacks are ignored.
            try:
                if self.js:
                    self.js.discarded = True
            except Exception:
                pass
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
            # Update the address bar safely across Tk/Skia modes
            try:
                self.browser.update_address(str(url))
            except Exception:
                pass
            # Update padlock icon (will skip in Skia mode if label absent)
            try:
                self.browser.update_padlock()
            except Exception:
                pass
            # Trigger a redraw via the active backend
            try:
                self.browser.draw()
            except Exception:
                pass
            # Refresh tab strip in Tk mode only (tabbar exists)
            if hasattr(self.browser, 'tabbar'):
                try:
                    self.browser.refresh_tab_strip()
                except Exception:
                    pass

    def render(self):
        """
        Compute style, layout, and paint for this tab's DOM, producing a
        display list. If the tab does not need rendering (needs_render
        is False), this method returns immediately. It also runs any
        requestAnimationFrame callbacks before rendering so that
        animations take effect.
        """
        # Skip render if nothing has changed
        if hasattr(self, 'needs_render') and not self.needs_render:
            return
        # Run any JavaScript requestAnimationFrame handlers before
        # performing style/layout/paint. This lets scripts modify the
        # DOM immediately before the frame is drawn.
        if self.js:
            try:
                self.js.interp.evaljs("__runRAFHandlers()")
            except Exception:
                pass
        # Clear widget hit-test boxes
        Browser._clear_widget_boxes()
        # Layout and paint
        self.document = DocumentLayout(self.nodes)
        self.document.layout()
        self.display_list = []
        paint_tree(self.document, self.display_list)
        self.doc_height = self.document.height
        # Clamp scroll offset to content height
        self.scroll = min(self.scroll, max(0, self.doc_height - HEIGHT))
        # Reset dirty flag
        self.needs_render = False
        # After layout and painting, trigger a redraw. On the next
        # animation frame the browser will repaint; however, to ensure
        # that the updated display list appears promptly in both Tk and
        # Skia modes, invoke the browser's draw method directly.
        try:
            if hasattr(self.browser, 'draw'):
                self.browser.draw()
        except Exception:
            pass

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
        # Ensure the layout and style are up to date before hit testing.
        # Without this call, clicks may be handled using a stale layout tree.
        try:
            self.render()
        except Exception:
            pass
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
            # Handle input elements: toggle checkboxes or focus text inputs
            if elt.tag == "input":
                # checkbox click toggles; text input focuses
                if elt.attributes.get("type", "text").lower() == "checkbox":
                    # toggle internal checked state
                    if ("checked" in elt.attributes) or (elt.attributes.get("_checked_state") == "true"):
                        # uncheck
                        if "checked" in elt.attributes:
                            del elt.attributes["checked"]
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

            # Handle buttons: find containing form and submit it
            elif elt.tag == "button":
                form = elt.parent
                while form and not (isinstance(form, Element) and form.tag == "form"):
                    form = form.parent
                if form:
                    self.submit_form(form)
                    return

            # Handle anchor (<a>) clicks by navigating to the href
            elif elt.tag == "a":
                try:
                    href = elt.attributes.get("href")
                    if href:
                        # Resolve the href relative to the current page URL
                        dest = self.url.resolve(href)
                        self.load(dest)
                        return
                except Exception:
                    pass

        # Fall back: re-render the page when clicking non-interactive areas
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

    def allowed_request(self, url: 'URL') -> bool:
        """
        Check whether a request to the given URL is allowed under the current
        Content-Security-Policy. If allowed_origins is None, all requests are
        allowed. Otherwise only requests whose origin is in allowed_origins
        are permitted.
        """
        if self.allowed_origins is None:
            return True
        try:
            origin = url.origin()
        except Exception:
            return False
        return origin in self.allowed_origins

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
                        except Exception:
                            script_url = None
                        # Skip if blocked by CSP
                        if script_url and not self.allowed_request(script_url):
                            # Disallowed by CSP: ignore script
                            self.loaded_scripts.add(src)
                        elif script_url:
                            try:
                                # Fetch script with referer and origin headers
                                ref = str(self.url) if self.url else None
                                origin = self.url.origin() if self.url else None
                                h, body = script_url.request(referrer=ref, payload=None, origin=origin)
                                # Instead of running the script immediately,
                                # schedule a task to run it later. This keeps
                                # the UI responsive and defers script
                                # execution until after loading completes.
                                try:
                                    task = Task(self.js.run, body)
                                    self.task_runner.schedule_task(task)
                                except Exception:
                                    pass
                                # Mark this script as loaded to avoid reloading
                                self.loaded_scripts.add(src)
                            except Exception:
                                # Network error: ignore
                                self.loaded_scripts.add(src)
                                pass
                # Process <link rel="stylesheet" href="...">
                if node.tag == "link" and node.attributes.get("rel", "").casefold() == "stylesheet" and "href" in node.attributes:
                    href = node.attributes["href"]
                    # Resolve URL; may raise
                    try:
                        css_url = self.url.resolve(href)
                    except Exception:
                        css_url = None
                    # Check CSP: skip blocked styles
                    if css_url and not self.allowed_request(css_url):
                        # Do not load or parse
                        continue
                    # Fetch and parse CSS for new or changed links
                    if node not in self.loaded_styles:
                        rules: list[tuple[object, dict[str, str]]] = []
                        if css_url:
                            try:
                                ref = str(self.url) if self.url else None
                                origin_header = self.url.origin() if self.url else None
                                h, css_body = css_url.request(referrer=ref, payload=None, origin=origin_header)
                                parser = CSSParser(css_body)
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

class SkiaCanvasAdapter:
    """Adapter exposing Tk-like canvas calls over a Skia canvas"""
    def __init__(self, canvas, y_offset=0):
        self.canvas = canvas
        self.y_offset = y_offset

    def create_rectangle(self, x1, y1, x2, y2, width=0, fill=None, outline=None):
        paint = skia.Paint(AntiAlias=True)
        if fill and fill != "":
            paint.setColor(_parse_color(fill))
            paint.setStyle(skia.Paint.kFill_Style)
            self.canvas.drawRect(skia.Rect.MakeLTRB(x1, y1 + self.y_offset, x2, y2 + self.y_offset), paint)
        if outline and outline != "":
            paint = skia.Paint(AntiAlias=True)
            paint.setColor(_parse_color(outline))
            paint.setStyle(skia.Paint.kStroke_Style)
            paint.setStrokeWidth(max(1, width or 1))
            self.canvas.drawRect(skia.Rect.MakeLTRB(x1, y1 + self.y_offset, x2, y2 + self.y_offset), paint)

    def create_line(self, x1, y1, x2, y2, fill=None, width=1):
        paint = skia.Paint(AntiAlias=True)
        if fill: paint.setColor(_parse_color(fill))
        paint.setStrokeWidth(max(1, width))
        self.canvas.drawLine(x1, y1 + self.y_offset, x2, y2 + self.y_offset, paint)

    def create_text(self, x, y, text, fill=None, anchor="nw", font=None):
        # font from Tk not usable; use skia default with approx size
        size = 14
        try:
            if isinstance(font, tuple) and len(font)>=2 and isinstance(font[1], int):
                size = font[1]
        except Exception:
            pass
        # Use our helper to avoid deprecated default fonts
        sk_font = _get_skia_font(size)
        paint = skia.Paint(AntiAlias=True)
        if fill:
            paint.setColor(_parse_color(fill))
        # Tk "nw" anchor: draw at (x,y)
        self.canvas.drawString(text, x, y + self.y_offset, sk_font, paint)

    def create_arc(self, x1, y1, x2, y2, start=0, extent=90, outline=None, fill=None):
        # approximate Tk pies with path arcs
        rect = skia.Rect.MakeLTRB(x1, y1 + self.y_offset, x2, y2 + self.y_offset)
        # Skia angles are in degrees, use drawArc for outline; fill pie via path
        if fill:
            path = skia.Path()
            cx = (x1 + x2)/2; cy = (y1 + y2)/2 + self.y_offset
            path.moveTo(cx, cy)
            path.arcTo(rect, start, extent, False)
            path.close()
            self.canvas.drawPath(path, skia.Paint(Color=_parse_color(fill), AntiAlias=True))
        if outline:
            paint = skia.Paint(Color=_parse_color(outline), Style=skia.Paint.kStroke_Style, AntiAlias=True)
            self.canvas.drawArc(rect, start, extent, False, paint)

class SkiaRenderer:
    def __init__(self, browser):
        self.browser = browser
        # Initialize SDL for both video and events. Using SDL_INIT_VIDEO ensures
        # that window creation functions work properly; SDL_INIT_EVENTS alone
        # may be insufficient on some platforms.
        sdl2.SDL_Init(sdl2.SDL_INIT_VIDEO | sdl2.SDL_INIT_EVENTS)
        flags = sdl2.SDL_WINDOW_SHOWN | sdl2.SDL_WINDOW_ALLOW_HIGHDPI
        self.window = sdl2.SDL_CreateWindow(b"WBE Browser (Skia)",
            sdl2.SDL_WINDOWPOS_CENTERED, sdl2.SDL_WINDOWPOS_CENTERED, WIDTH, HEIGHT, flags)
        self.chrome_h = 44
        self.surface_root = skia.Surface(WIDTH, HEIGHT)
        self.surface_tab = skia.Surface(WIDTH, HEIGHT)   # big enough; clip+translate when drawing
        self.addr_text = ""
        self.btn_back = (8, 8, 36, 36)
        self.btn_fwd = (40, 8, 68, 36)
        self.btn_reload = (72, 8, 100, 36)
        self.addr_rect = (108, 10, WIDTH - 12, 34)
        self.sdl_surface = sdl2.SDL_GetWindowSurface(self.window)

        # Store HTTPS secure state for padlock display
        self._secure = False

    def draw_frame(self):
        # clear root
        rootc = self.surface_root.getCanvas()
        rootc.clear(skia.ColorWHITE)

        # draw chrome
        rootc.drawRect(skia.Rect.MakeLTRB(0, 0, WIDTH, self.chrome_h),
                       skia.Paint(Color=_parse_color("#f1f5f9")))
        self._draw_button(rootc, self.btn_back, "←")
        self._draw_button(rootc, self.btn_fwd, "→")
        self._draw_button(rootc, self.btn_reload, "⟳")
        self._draw_address(rootc, self.addr_rect, self.addr_text)

        # Draw padlock icon when the current page is secure (HTTPS with no certificate errors)
        try:
            if getattr(self, '_secure', False):
                # Use a non-deprecated font for the lock glyph
                pad_font = _get_skia_font(14)
                # Draw the lock just to the left of the address bar
                px = self.addr_rect[0] - 20
                py = self.addr_rect[1] + 14
                rootc.drawString("\N{lock}", px, py, pad_font,
                                  skia.Paint(Color=_parse_color("#111111")))
        except Exception:
            # If anything goes wrong (e.g., typeface unavailable), ignore
            pass

        # draw tab contents on tab surface
        tabc = self.surface_tab.getCanvas()
        tabc.clear(skia.ColorWHITE)

        # Let browser build its display list (same as Tk path) and execute on a SkiaCanvasAdapter
        # Clear previously registered widget boxes so hit-testing only uses
        # elements from the current frame
        try:
            Browser._clear_widget_boxes()
        except Exception:
            pass
        cmds = self.browser.build_display_list()
        adapter = SkiaCanvasAdapter(tabc, y_offset=-self.browser.active_tab.scroll + self.chrome_h)
        for cmd in cmds:
            try:
                cmd.execute(0, adapter)  # scroll already encoded in adapter
            except Exception:
                pass

        # composite: draw tab surface into root clipped under chrome
        rootc.save()
        rootc.clipRect(skia.Rect.MakeLTRB(0, self.chrome_h, WIDTH, HEIGHT))
        self.surface_tab.draw(rootc, 0, 0)
        rootc.restore()

        # present to window
        self._present()

        rnd = getattr(self.browser, "renderer", None)
        if rnd is not None and hasattr(rnd, "update_address_bar"):
            try: rnd.update_address_bar()
            except Exception: pass

        # Optional status bar (bottom-left)
        try:
            txt = getattr(self, "status_text", "")
            if txt:
                rootc = self.surface_root.getCanvas()
                font = _get_skia_font(12)
                paint_bg = skia.Paint(Color=_parse_color("#222222"))
                paint_fg = skia.Paint(Color=_parse_color("#ffffff"))
                # simple strip
                rootc.drawRect(skia.Rect.MakeLTRB(0, HEIGHT-20, WIDTH, HEIGHT), paint_bg)
                rootc.drawString(txt, 8, HEIGHT-6, font, paint_fg)
        except Exception:
            pass

    def _present(self):
        """
        Present the current Skia frame onto the SDL surface backing our window.

        This copies pixels from the Skia root surface into the SDL surface memory
        using Image.readPixels and the buffer protocol. Using readPixels avoids
        the need to construct a Pixmap with a ctypes pointer, which can cause
        "Buffer does not have dimensions" errors on some versions of skia‑python.
        """
        # Lock the SDL surface for direct pixel access
        sdl2.SDL_LockSurface(self.sdl_surface)
        try:
            # On some platforms SDL_GetWindowSurface returns a pointer to a
            # structure; on others it returns the structure directly. Handle both.
            sf = self.sdl_surface.contents
        except AttributeError:
            sf = self.sdl_surface

        # Create a Skia ImageInfo describing the SDL surface
        info = skia.ImageInfo.Make(sf.w, sf.h,
                                   skia.ColorType.kRGBA_8888_ColorType,
                                   skia.AlphaType.kPremul_AlphaType)
        # Gather pixel buffer information from SDL
        height = getattr(sf, 'h', 0)
        row_bytes = getattr(sf, 'pitch', 0)
        pixels_ptr = getattr(sf, 'pixels', None)

        if pixels_ptr and height and row_bytes:
            # Compute the total size of the pixel buffer
            size = row_bytes * height
            # Construct a ctypes array that points into the SDL surface memory
            buf_type = ctypes.c_ubyte * size
            buf = buf_type.from_address(pixels_ptr)
            # Wrap in a memoryview so skia can treat it as a Python buffer
            mv = memoryview(buf)
            # Take a snapshot of the current Skia surface
            img = self.surface_root.makeImageSnapshot()
            # Copy pixels into the SDL buffer. We ignore the return value,
            # as it may return False if readPixels fails.
            try:
                img.readPixels(info, mv, row_bytes)
            except Exception:
                # If readPixels raises, silently ignore; we'll still unlock surface
                pass

        # Unlock the surface and update the window
        sdl2.SDL_UnlockSurface(self.sdl_surface)
        sdl2.SDL_UpdateWindowSurface(self.window)

    def _in(self, x, y, r): x1,y1,x2,y2 = r; return x1 <= x <= x2 and y1 <= y <= y2

    def _draw_button(self, canvas, rect, label):
        x1,y1,x2,y2 = rect
        rr = skia.RRect.MakeRectXY(skia.Rect.MakeLTRB(x1,y1,x2,y2), 6,6)
        canvas.drawRRect(rr, skia.Paint(Color=_parse_color("#e5e7eb")))
        # Use non-deprecated font for button label
        # Use helper to avoid deprecated default font
        fnt = _get_skia_font(16)
        canvas.drawString(label, x1+10, y1+12, fnt, skia.Paint(Color=_parse_color("#111111")))

    def _draw_address(self, canvas, rect, text):
        x1,y1,x2,y2 = rect
        rr = skia.RRect.MakeRectXY(skia.Rect.MakeLTRB(x1,y1,x2,y2), 8,8)
        canvas.drawRRect(rr, skia.Paint(Color=_parse_color("#ffffff")))
        # Use helper to avoid deprecated default font for address text
        fnt = _get_skia_font(14)
        canvas.drawString(text, x1+10, y1+14, fnt, skia.Paint(Color=_parse_color("#111111")))

    def mainloop(self):
        event = sdl2.SDL_Event()
        sdl2.SDL_StartTextInput()
        # set initial address
        if self.browser.active_tab and self.browser.active_tab.url:
            self.addr_text = str(self.browser.active_tab.url)
        while True:
            while sdl2.SDL_PollEvent(ctypes.byref(event)) != 0:
                if event.type == sdl2.SDL_QUIT:
                    self._quit()
                elif event.type == sdl2.SDL_MOUSEBUTTONUP:
                    x, y = event.button.x, event.button.y
                    if y <= self.chrome_h:
                        if self._in(x,y,self.btn_back): self.browser.back()
                        elif self._in(x,y,self.btn_fwd): self.browser.forward()
                        elif self._in(x,y,self.btn_reload): self.browser.reload()
                        elif self._in(x,y,self.addr_rect): pass  # focus not tracked
                    else:
                        self.browser.active_tab.click(x, y - self.chrome_h + self.browser.active_tab.scroll)
                elif event.type == sdl2.SDL_KEYDOWN:
                    sym = event.key.keysym.sym
                    if sym == sdl2.SDLK_RETURN:
                        url = self.addr_text.strip()
                        if url and not url.startswith("http"):
                            url = "https://" + url
                        if url:
                            try:
                                # Load the new URL into the active tab (this also
                                # updates the address bar via update_address)
                                self.browser.active_tab.load(URL(url))
                            except Exception:
                                pass
                            # Ensure the address bar text matches the URL
                            try:
                                self.browser.update_address(url)
                            except Exception:
                                pass
                    elif sym == sdl2.SDLK_BACKSPACE:
                        self.addr_text = self.addr_text[:-1]
                    elif sym == sdl2.SDLK_DOWN:
                        self.browser.active_tab.scrolldown()
                    elif sym == sdl2.SDLK_UP:
                        self.browser.active_tab.scrollup()
                elif event.type == sdl2.SDL_TEXTINPUT:
                    self.addr_text += event.text.text.decode('utf8')
            # Run one pending task for the active tab before drawing
            try:
                tab = self.browser.active_tab
                if hasattr(tab, 'task_runner'):
                    tab.task_runner.run()
            except Exception:
                pass
            self.draw_frame()
            sdl2.SDL_Delay(10)

    def _quit(self):
        if self.window: sdl2.SDL_DestroyWindow(self.window)
        sdl2.SDL_Quit()
        sys.exit(0)

    def update_address_bar(self):
        if self.browser.active_tab and self.browser.active_tab.url:
            self.addr_text = str(self.browser.active_tab.url) 

    # Allow Browser to store HTTPS secure state
    def set_padlock(self, secure: bool):
        self._secure = bool(secure)

    def set_status(self, msg):
        # Option A: just store it and draw at bottom in draw_frame()
        self.status_text = msg

class Browser:

    def build_display_list(self):
        """Collect display commands from the layout tree (Draw* objects)."""
        cmds = []
        if self.active_tab and getattr(self.active_tab, "document", None):
            def collect(layout):
                if hasattr(layout, "should_paint") and layout.should_paint():
                    cmds.extend(layout.paint())
                for c in getattr(layout, "children", []): collect(c)
            collect(self.active_tab.document)
        return cmds

    def draw_for_renderer(self, renderer):
        """Used by non-Tk renderers to trigger a paint without Tk canvas assumptions."""
        if hasattr(renderer, "draw_frame"):
            renderer.draw_frame()
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
        # Flags used for task scheduling and rendering cadence. See Chapter 12.
        # If True, the next call to raster_and_draw() should repaint the
        # browser's chrome and active tab. Tabs set this flag when their
        # display list changes (e.g., after a render).
        self.needs_raster_and_draw: bool = False
        # Indicates whether an animation frame task should be scheduled. When a
        # web page calls requestAnimationFrame or when a tab marks itself as
        # needing render, this flag is set. The browser uses it to avoid
        # scheduling multiple render tasks in a row.
        self.needs_animation_frame: bool = True
        # Handle to an in-flight timer that schedules the next animation frame.
        self.animation_timer = None
        # --- choose renderer early (GPU preferred) ---
        global TK_ACTIVE
        use_skia = False
        try:
            use_skia = bool(SKIA_OK)
        except Exception:
            use_skia = False

        if use_skia:
            TK_ACTIVE = False
            # No Tk windows at all in Skia mode
            self.renderer = SkiaRenderer(self)
            # Minimal core state consistent with the rest of your code
            self.tabs = []
            self.active_tab_index = 0
            self.active_tab = Tab(self)
            self.tabs.append(self.active_tab)
            self._status_text = ""   # allow set_status in Skia mode
            return  # IMPORTANT: skip the Tk UI setup below

        # --- Tk fallback path (unchanged from your code) ---
        TK_ACTIVE = True
        self.window = tkinter.Tk()
        self.chrome_ctl = Chrome(self)

        # --- tab strip ---
        self.tabbar = tkinter.Frame(self.window, bg="#e6e6e6")
        self.tabbar.pack(fill="x")
        self.tabs = []
        self.active_tab_index = 0

        self.scrollbar_thumb = None
        self._dragging_scroll = False
        self._drag_offset = 0
        self._scroll_velocity = 0.0
        self._scroll_animating = False
        self._status_text = ""

        # --- chrome bar ---
        self.chrome = tkinter.Frame(self.window)
        self.back_btn  = tkinter.Button(self.chrome, text="◀", width=2, command=self.go_back)
        self.fwd_btn   = tkinter.Button(self.chrome, text="▶", width=2, command=self.go_forward)
        self.reload_btn= tkinter.Button(self.chrome, text="⟳", width=2, command=self.reload)
        self.padlock = tkinter.Label(self.chrome, text="", width=2)
        self.address   = tkinter.Entry(self.chrome, width=60)
        self.address.configure(bg="white", fg="black", insertbackground="black", highlightthickness=0)  # readable
        self.go_btn    = tkinter.Button(self.chrome, text="Go", command=self.go_address)
        self.back_btn.pack(side="left"); self.fwd_btn.pack(side="left"); self.reload_btn.pack(side="left")
        self.padlock.pack(side="left")
        self.address.pack(side="left", fill="x", expand=True, padx=4)
        self.go_btn.pack(side="left")
        self.chrome.pack(fill="x")
        self.chrome_ctl.bottom = self.chrome.winfo_reqheight() + self.tabbar.winfo_reqheight()

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

        self._bind_accels()

        # first tab (Tk path)
        self.new_tab(URL("https://browser.engineering/chrome.html"))

    def update_padlock(self) -> None:
        """
        Update the padlock icon in the address bar. Displays a lock icon (🔒)
        if the current tab is on HTTPS and has no certificate errors;
        otherwise clears the icon. Called after page loads and when
        certificate errors occur.
        """
        try:
            tab = self.current_tab()
        except Exception:
            return
        # Determine if a padlock should be displayed
        secure = False
        url = getattr(tab, 'url', None)
        if isinstance(url, URL):
            secure = (url.scheme == 'https' and not getattr(tab, 'cert_error', False))

        # Update Tk padlock label if present
        pad = getattr(self, 'padlock', None)
        if pad is not None:
            try:
                pad.config(text=("\N{lock}" if secure else ""))
            except Exception:
                pass

        # In the Skia renderer, optionally store secure state for drawing
        rnd = getattr(self, 'renderer', None)
        if rnd is not None and hasattr(rnd, 'set_padlock'):
            try:
                rnd.set_padlock(secure)
            except Exception:
                pass

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
        # Only refresh tab strip in Tk mode
        if hasattr(self, 'tabbar'):
            self.refresh_tab_strip()
        if url:
            tab.navigate(url)
        # Redraw page via active renderer
        self.draw()

    def switch_tab(self, idx: int):
        if 0 <= idx < len(self.tabs):
            self.active_tab_index = idx
            tab = self.current_tab()
            if 0 <= tab.history_index < len(tab.history):
                url = tab.history[tab.history_index]["url"]
                try:
                    # Display the URL string across Tk/Skia modes
                    self.update_address(str(url))
                except Exception:
                    pass
            # Refresh tab strip only in Tk mode
            if hasattr(self, 'tabbar'):
                try:
                    self.refresh_tab_strip()
                except Exception:
                    pass
            # Redraw page via active renderer
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
        # Refresh tab strip only in Tk mode
        if hasattr(self, 'tabbar'):
            try:
                self.refresh_tab_strip()
            except Exception:
                pass
        # Redraw page via active renderer
        self.draw()

    def refresh_tab_strip(self):
        # Skip tab strip refresh in Skia mode (no tabbar)
        tabbar = getattr(self, 'tabbar', None)
        if tabbar is None:
            return
        # Clear existing tab buttons
        for w in tabbar.winfo_children():
            w.destroy()
        # Recreate tab buttons
        for i, t in enumerate(self.tabs):
            cell = tkinter.Frame(tabbar, bd=0, relief="flat", bg="#e6e6e6")
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
        plus = tkinter.Button(tabbar, text="+", width=3,
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
    # Store status even when Tk widgets don't exist (Skia mode)
    def set_status(self, msg):
        # always keep the latest message
        self._status_text = msg

        # Update Tk label if present
        lbl = getattr(self, "status", None)
        if lbl is not None:
            try:
                lbl.config(text=msg)
            except Exception:
                pass

        # Notify Skia renderer if present
        r = getattr(self, "renderer", None)
        if r is not None and hasattr(r, "set_status"):
            try:
                r.set_status(msg)
            except Exception:
                pass

    def update_address(self, url_str: str) -> None:
        """
        Safely update the address bar across both Tk and Skia modes.

        :param url_str: The string to display in the address bar.
        """
        # Update Tk Entry if present
        addr = getattr(self, 'address', None)
        if addr is not None:
            try:
                addr.delete(0, 'end')
                addr.insert(0, url_str)
            except Exception:
                pass
        # Update Skia address bar text directly
        rnd = getattr(self, 'renderer', None)
        if rnd is not None:
            try:
                # Set the text; renderer will use this on next draw_frame
                rnd.addr_text = url_str
            except Exception:
                pass


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
        """
        Paint the current page, delegating to the active renderer.

        In Skia mode (when a renderer with a draw_frame method exists), this
        delegates drawing to the Skia renderer and returns immediately. In
        Tk mode, it clears the Tk canvas and executes the display list to
        draw widgets and the scrollbar.
        """
        # Before drawing, run one pending task for the active tab (if any).
        try:
            tab = self.current_tab()
            if hasattr(tab, 'task_runner'):
                tab.task_runner.run()
        except Exception:
            pass

        # If a Skia renderer is active, let it handle drawing
        rnd = getattr(self, "renderer", None)
        if rnd is not None and hasattr(rnd, "draw_frame"):
            try:
                rnd.draw_frame()
                return
            except Exception:
                pass
        # Tk fallback: draw on canvas
        tab = self.current_tab()
        canvas = getattr(self, "canvas", None)
        if canvas is None:
            return
        try:
            canvas.delete("all")
            for cmd in tab.display_list:
                cmd.execute(tab.scroll, canvas)
            self.draw_scrollbar(tab)
        except Exception:
            pass

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

    # ---- scheduling and rendering utilities ----
    def set_needs_raster_and_draw(self) -> None:
        """
        Mark that the browser chrome and active tab need to be redrawn. This
        sets a dirty flag so that the next call to raster_and_draw will
        actually repaint. Tabs should call this after they change their
        display list.
        """
        self.needs_raster_and_draw = True

    def raster_and_draw(self) -> None:
        """
        Repaint the browser chrome and active tab if needed. This checks
        the dirty flag set by set_needs_raster_and_draw and calls the
        appropriate drawing routine (Tk or Skia). After painting, the
        dirty flag is reset. If no repaint is needed, this returns
        immediately.
        """
        if not getattr(self, 'needs_raster_and_draw', False):
            return
        try:
            # Delegate to Skia renderer if present
            rnd = getattr(self, 'renderer', None)
            if rnd is not None and hasattr(rnd, 'draw_frame'):
                rnd.draw_frame()
            else:
                # Fallback to Tk drawing
                self.draw()
        except Exception:
            pass
        # Clear the dirty flag
        self.needs_raster_and_draw = False

    def set_needs_animation_frame(self, tab: 'Tab') -> None:
        """
        Indicate that a new animation frame should be scheduled for the given
        tab. Only the active tab can schedule a new animation frame; calls
        from inactive tabs are ignored to avoid interfering with visible
        content.
        """
        try:
            if tab is getattr(self, 'active_tab', None):
                self.needs_animation_frame = True
        except Exception:
            pass

    def schedule_animation_frame(self) -> None:
        """
        Schedule a task to render the active tab on the next animation
        frame. This sets up a timer that will execute after
        REFRESH_RATE_SEC seconds and enqueues a render task on the
        active tab's task runner. If an animation frame is already
        scheduled or no frame is needed, this returns immediately.
        """
        try:
            if not getattr(self, 'needs_animation_frame', False):
                return
            if getattr(self, 'animation_timer', None) is not None:
                return

            def callback():
                # After the delay, enqueue a render task on the active tab
                try:
                    active_tab = getattr(self, 'active_tab', None)
                    if active_tab and hasattr(active_tab, 'task_runner'):
                        task = Task(active_tab.render)
                        active_tab.task_runner.schedule_task(task)
                except Exception:
                    pass
                # Reset timer and flag
                self.animation_timer = None
                self.needs_animation_frame = False
            # Start a new timer for the next animation frame
            self.animation_timer = threading.Timer(REFRESH_RATE_SEC, callback)
            self.animation_timer.start()
        except Exception:
            pass

    def run(self, url):
        # Ensure we have a tab (works for both Skia and Tk paths)
        if not getattr(self, "tabs", None):
            self.tabs = []
            self.active_tab_index = 0
            self.active_tab = Tab(self)
            self.tabs.append(self.active_tab)

        # Load the start URL
        self.active_tab.load(URL(url))

        # If a non-Tk renderer (Skia) is active, use its event loop
        if hasattr(self, "renderer") and hasattr(self.renderer, "mainloop"):
            self.renderer.mainloop()
            return

        # Otherwise, fall back to Tk’s loop
        self.window.mainloop()



# ================= CLI =================
if __name__ == "__main__":
    import sys

    # Optional: force Tk fallback for testing with --tk / --use-tk
    force_tk = any(a in ("--tk", "--use-tk") for a in sys.argv)
    if force_tk:
        try:
            SKIA_OK = False  # override Skia availability
        except NameError:
            pass
        # remove the flag from argv
        sys.argv = [a for a in sys.argv if a not in ("--tk", "--use-tk")]

    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.org"

    browser = Browser()
    browser.run(url)  # will choose Skia mainloop if available, else Tk

