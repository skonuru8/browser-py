# browser5.py
import socket, ssl, tkinter, tkinter.font

# -------------------- Networking (Ch.1) --------------------
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
        _ = resp.readline()  # status line
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

# -------------------- HTML nodes & parser (Ch.4) --------------------
class Text:
    def __init__(self, text, parent):
        self.text = text
        self.children = []
        self.parent = parent
    def __repr__(self): return repr(self.text)

class Element:
    def __init__(self, tag, attributes, parent):
        self.tag = tag
        self.attributes = attributes
        self.children = []
        self.parent = parent
    def __repr__(self): return "<" + self.tag + ">"

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
        text = ""
        in_tag = False
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
            self.implicit_tags(None)
            parent = self.unfinished[-1]
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

# -------------------- Fonts & helpers (Ch.3) --------------------
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

# -------------------- Layout tree (Ch.5) --------------------
class DocumentLayout:
    def __init__(self, node):
        self.node = node
        self.parent = None
        self.children = []
        self.x = self.y = self.width = self.height = None

    def layout(self):
        child = BlockLayout(self.node, self, None)
        self.children.append(child)
        self.width = WIDTH - 2*HSTEP
        self.x = HSTEP
        self.y = VSTEP
        child.layout()
        self.height = child.height

    def paint(self):
        return []

class BlockLayout:
    def __init__(self, node, parent, previous):
        self.node = node
        self.parent = parent
        self.previous = previous
        self.children = []
        self.display_list = []  # (x,y,word,font) for inline blocks
        self.x = self.y = self.width = self.height = None
        # inline state (for leaf inline blocks)
        self.cursor_x = self.cursor_y = 0
        self.weight = "normal"; self.style = "roman"; self.size = 12
        self.line = []

    def layout_mode(self):
        if isinstance(self.node, Text):
            return "inline"
        elif any([isinstance(child, Element) and
                  child.tag in BLOCK_ELEMENTS
                  for child in self.node.children]):
            return "block"
        elif self.node.children:
            return "inline"
        else:
            return "block"

    def layout(self):
        # Compute this block's x/width and y before recursing
        self.x = self.parent.x
        self.width = self.parent.width
        self.y = (self.previous.y + self.previous.height) if self.previous else self.parent.y

        mode = self.layout_mode()
        if mode == "block":
            previous = None
            for child in self.node.children:
                next = BlockLayout(child, self, previous)
                self.children.append(next)
                previous = next
        else:
            # inline leaf: lay out words relative to block origin
            self.cursor_x = 0; self.cursor_y = 0
            self.weight = "normal"; self.style = "roman"; self.size = 12
            self.line = []
            self.recurse(self.node)
            self.flush()

        # Recurse into children to compute their layout
        for child in self.children:
            child.layout()

        # Compute height
        if mode == "block":
            self.height = sum(child.height for child in self.children) if self.children else VSTEP
        else:
            # inline height = last baseline + descent (estimated from fonts)
            if self.display_list:
                # pick a representative font metrics
                any_font = self.display_list[0][3]
                self.height = (self.display_list[-1][1] - self.y) + any_font.metrics("linespace")
            else:
                self.height = VSTEP

    def open_tag(self, tag):
        if tag == "i": self.style = "italic"
        elif tag == "b": self.weight = "bold"
        elif tag == "small": self.size -= 2
        elif tag == "big": self.size += 4
        elif tag == "br": self.flush()

    def close_tag(self, tag):
        if tag == "i": self.style = "roman"
        elif tag == "b": self.weight = "normal"
        elif tag == "/p":
            self.flush(); self.cursor_y += VSTEP
        elif tag == "/small": self.size += 2
        elif tag == "/big": self.size -= 4

    def recurse(self, node):
        if isinstance(node, Text):
            for w in node.text.split():
                self.word(w)
        else:
            self.open_tag(node.tag)
            for child in node.children:
                self.recurse(child)
            self.close_tag(node.tag)

    def word(self, word):
        font = get_font(self.size, self.weight, self.style)
        w = font.measure(word)
        if self.cursor_x + w > self.width:
            self.flush()
        # store (relative x, word, font); y is baseline-adjusted in flush
        self.line.append((self.cursor_x, word, font))
        self.cursor_x += w + font.measure(" ")

    def flush(self):
        if not self.line: return
        metrics = [font.metrics() for _, _, font in self.line]
        max_ascent = max(m["ascent"] for m in metrics)
        max_descent = max(m["descent"] for m in metrics)
        baseline = self.cursor_y + max_ascent
        for rel_x, word, font in self.line:
            x = self.x + rel_x
            y = self.y + baseline - font.metrics("ascent")
            self.display_list.append((x, y, word, font))
        self.cursor_y = baseline + int(1.25 * max_descent)
        self.cursor_x = 0
        self.line = []

    def paint(self):
        cmds = []
        # Background example: gray for <pre> blocks
        if isinstance(self.node, Element) and self.node.tag == "pre":
            x2, y2 = self.x + self.width, self.y + self.height
            cmds.append(DrawRect(self.x, self.y, x2, y2, "gray"))
        # Inline text becomes DrawText commands
        if self.layout_mode() == "inline":
            for x, y, word, font in self.display_list:
                cmds.append(DrawText(x, y, word, font))
        return cmds

BLOCK_ELEMENTS = [
    "html","body","article","section","nav","aside",
    "h1","h2","h3","h4","h5","h6","hgroup","header",
    "footer","address","p","hr","pre","blockquote",
    "ol","ul","menu","li","dl","dt","dd","figure",
    "figcaption","main","div","table","form","fieldset",
    "legend","details","summary"
]

# -------------------- Draw commands (Ch.5) --------------------
class DrawText:
    def __init__(self, x1, y1, text, font):
        self.top = y1; self.left = x1
        self.text = text; self.font = font
    def execute(self, scroll, canvas):
        canvas.create_text(self.left, self.top - scroll,
                           text=self.text, font=self.font, anchor='nw')

class DrawRect:
    def __init__(self, x1, y1, x2, y2, color):
        self.top = y1; self.left = x1
        self.bottom = y2; self.right = x2
        self.color = color
    def execute(self, scroll, canvas):
        canvas.create_rectangle(self.left, self.top - scroll,
                                self.right, self.bottom - scroll,
                                width=0, fill=self.color)

def paint_tree(layout_object, display_list):
    display_list.extend(layout_object.paint())
    for child in layout_object.children:
        paint_tree(child, display_list)

# -------------------- GUI --------------------
class Browser:
    def __init__(self):
        self.window = tkinter.Tk()
        self.canvas = tkinter.Canvas(self.window, width=WIDTH, height=HEIGHT)
        self.canvas.pack()
        self.scroll = 0
        self.display_list = []
        self.window.bind("<Down>", self.scrolldown)

    def load(self, url):
        body = url.request()
        nodes = HTMLParser(body).parse()
        self.document = DocumentLayout(nodes)
        self.document.layout()
        self.display_list = []
        paint_tree(self.document, self.display_list)
        self.draw()

    def draw(self):
        self.canvas.delete("all")
        for cmd in self.display_list:
            cmd.execute(self.scroll, self.canvas)

    def scrolldown(self, e):
        self.scroll += SCROLL_STEP
        self.draw()

# -------------------- CLI --------------------
if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python3 browser5.py <URL>")
        raise SystemExit(1)
    Browser().load(URL(sys.argv[1]))
    tkinter.mainloop()
