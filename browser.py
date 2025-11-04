# browser_chrome_tabs_api.py
import socket, ssl, tkinter, tkinter.font

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

    def request(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        s.connect((self.host, self.port))
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            s = ctx.wrap_socket(s, server_hostname=self.host)
        req = f"GET {self.path} HTTP/1.0\r\nHost: {self.host}\r\n\r\n"
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

    # --- shim to match chapter API ---
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
    def __repr__(self): return repr(self.text)

class Element:
    def __init__(self, tag, attributes, parent):
        self.tag = tag
        self.attributes = attributes
        self.children = []
        self.parent = parent
        self.style = {}
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

def cascade_priority(rule):
    selector, _ = rule
    return selector.priority

# UA stylesheet as Python objects (robust)
DEFAULT_STYLE_SHEET = [
    (TagSelector("body"), {"background-color": "white", "color": "black"}),
    (TagSelector("pre"),  {"background-color": "gray"}),
    (DescendantSelector(TagSelector("body"), TagSelector("a")), {"color": "blue"}),
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

class BlockLayout:
    def __init__(self, node, parent, previous):
        self.node = node
        self.parent = parent
        self.previous = previous
        self.children = []
        self.display_list = []  # (x,y,word,font,color)
        self.x = self.y = self.width = self.height = None
        self.cursor_x = self.cursor_y = 0
        self.weight = "normal"; self.style = "roman"; self.size = 12
        self.line = []

    def layout_mode(self):
        if isinstance(self.node, Text): return "inline"
        elif any(isinstance(c, Element) and c.tag in BLOCK_ELEMENTS for c in self.node.children): return "block"
        elif self.node.children: return "inline"
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
            if self.display_list:
                any_font = self.display_list[0][3]
                self.height = (self.display_list[-1][1] - self.y) + any_font.metrics("linespace")
            else:
                self.height = VSTEP

    def recurse(self, node):
        if isinstance(node, Text):
            for w in node.text.split():
                self.word(node, w)
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
        self.line.append((self.cursor_x, word, font, color))
        self.cursor_x += w + font.measure(" ")

    def flush(self):
        if not self.line: return
        metrics = [font.metrics() for _, _, font, _ in self.line]
        max_ascent = max(m["ascent"] for m in metrics)
        max_descent = max(m["descent"] for m in metrics)
        baseline = self.cursor_y + max_ascent
        for rel_x, word, font, color in self.line:
            x = self.x + rel_x
            y = self.y + baseline - font.metrics("ascent")
            self.display_list.append((x, y, word, font, color))
        self.cursor_y = baseline + int(1.25 * max_descent)
        self.cursor_x = 0
        self.line = []

    def paint(self):
        cmds = []
        if isinstance(self.node, Element) and self.node.tag == "pre":
            x2, y2 = self.x + self.width, self.y + self.height
            cmds.append(DrawRect(self.x, self.y, x2, y2, "gray"))
        if self.layout_mode() == "inline":
            for x, y, word, font, color in self.display_list:
                cmds.append(DrawText(x, y, word, font, color))
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

# not used by our UI, but provided to match chapter API
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

def paint_tree(layout_object, display_list):
    display_list.extend(layout_object.paint())
    for child in layout_object.children:
        paint_tree(child, display_list)

# ================= Tab abstraction =================
class Tab:
    """
    Per-tab state & navigation.
    """
    def __init__(self, browser, home_url=None):
        self.browser = browser
        self.history = []
        self.history_index = -1
        self.nodes = None
        self.document = None
        self.display_list = []
        self.scroll = 0
        self.doc_height = HEIGHT
        self.title = "New Tab"
        if home_url: self.navigate(home_url)

    def navigate(self, url):
        if self.history_index + 1 < len(self.history):
            self.history = self.history[:self.history_index + 1]
        self.history.append(url)
        self.history_index += 1
        self.load(url)

    def go_back(self):
        if self.history_index > 0:
            self.history_index -= 1
            self.load(self.history[self.history_index], push_history=False)

    def go_forward(self):
        if self.history_index + 1 < len(self.history):
            self.history_index += 1
            self.load(self.history[self.history_index], push_history=False)

    def reload(self):
        if 0 <= self.history_index < len(self.history):
            self.load(self.history[self.history_index], push_history=False)

    def load(self, url, push_history=True):
        try:
            self.browser.set_status("Loading…")
            body = url.request()
            self.browser.set_status("")
        except Exception as ex:
            self.browser.set_status(f"Network error: {ex}")
            return

        self.nodes = HTMLParser(body).parse()
        self.title = self._extract_title() or f"{url.host}"

        rules = DEFAULT_STYLE_SHEET[:]
        rules.sort(key=cascade_priority)
        style(self.nodes, rules)

        self.document = DocumentLayout(self.nodes)
        self.document.layout()
        self.display_list = []
        paint_tree(self.document, self.display_list)
        self.doc_height = self.document.height
        self.scroll = 0

        if self is self.browser.current_tab():
            self.browser.address.delete(0, "end")
            self.browser.address.insert(0, str(url))
            self.browser.draw()
            self.browser.refresh_tab_strip()

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

# ================= Chrome shim (API parity, forwards to Browser) =================
class Chrome:
    def __init__(self, browser):
        self.browser = browser

    # simple rectangles for tab buttons (approx—our UI uses Tk buttons)
    def tab_rect(self, i):
        x0 = 6 + i * 140
        return Rect(x0, 2, x0 + 128, 28)

    def draw(self):
        # no-op: our Browser builds a real Tk tab strip; kept for API parity
        pass

    def click(self, x, y):
        # map a click to a tab index using tab_rects; then switch
        for i in range(len(self.browser.tabs)):
            r = self.tab_rect(i)
            if r.contains_point(x, y):
                self.browser.switch_tab(i); return

    def keypress(self, char):
        # forward to Browser address bar
        self.browser.address.insert("end", char)

    def enter(self):
        self.browser.go_address()

# ================= Browser (chrome + tabs) =================
class Browser:
    def __init__(self):
        self.window = tkinter.Tk()

        # chrome shim (not strictly used, but available)
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

        # --- canvas ---
        self.canvas = tkinter.Canvas(self.window, width=WIDTH, height=HEIGHT,
                                     background="white", highlightthickness=0)
        self.canvas.pack()

        # --- status ---
        self.status = tkinter.Label(self.window, text="", anchor="w")
        self.status.pack(fill="x")

        # bindings
        self.window.bind("<Return>", lambda e: self.go_address())
        self.window.bind("<Down>",   lambda e: self.scroll_active(+SCROLL_STEP))
        self.window.bind("<Up>",     lambda e: self.scroll_active(-SCROLL_STEP))
        self.window.bind("<Prior>",  lambda e: self.scroll_active(-int(HEIGHT*0.9)))
        self.window.bind("<Next>",   lambda e: self.scroll_active(+int(HEIGHT*0.9)))
        self.window.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<Button-4>", self.on_wheel_linux)
        self.canvas.bind("<Button-5>", self.on_wheel_linux)

        # create first tab
        self.new_tab(URL("https://browser.engineering/chrome.html"))

    # -------- tabs --------
    def current_tab(self) -> Tab:
        return self.tabs[self.active_tab_index]

    def new_tab(self, url: URL):
        tab = Tab(self)
        self.tabs.append(tab)
        self.active_tab_index = len(self.tabs) - 1
        self.refresh_tab_strip()
        self.draw()

    def switch_tab(self, idx: int):
        if 0 <= idx < len(self.tabs):
            self.active_tab_index = idx
            tab = self.current_tab()
            if 0 <= tab.history_index < len(tab.history):
                url = tab.history[tab.history_index]
                self.address.delete(0, "end")
                self.address.insert(0, str(url))
            self.refresh_tab_strip()
            self.draw()

    def close_tab(self, idx: int):
        if len(self.tabs) <= 1:  # keep at least one
            return
        del self.tabs[idx]
        if self.active_tab_index >= len(self.tabs):
            self.active_tab_index = len(self.tabs) - 1
        self.refresh_tab_strip()
        self.draw()

    def refresh_tab_strip(self):
        for w in self.tabbar.winfo_children(): w.destroy()
        for i, t in enumerate(self.tabs):
            title = t.title or "New Tab"
            b = tkinter.Button(self.tabbar, text=title[:24] + ("…" if len(title) > 24 else ""),
                               command=lambda j=i: self.switch_tab(j),
                               relief="sunken" if i == self.active_tab_index else "raised")
            b.pack(side="left", padx=2, pady=2)
        plus = tkinter.Button(self.tabbar, text="+", width=3,
                              command=lambda: self.new_tab(URL("https://example.org/")))
        plus.pack(side="left", padx=4, pady=2)

    # -------- chrome actions --------
    def set_status(self, msg): self.status.config(text=msg)

    def go_address(self):
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
        self.scroll_active(-int(e.delta / 120) * 40)

    def on_wheel_linux(self, e):
        self.scroll_active(-40 if e.num == 4 else +40)

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
    import sys
    app = Browser()
    if len(sys.argv) == 2:
        app.current_tab().navigate(URL(sys.argv[1]))
    tkinter.mainloop()
