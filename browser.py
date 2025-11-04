# browser3.py
import socket, ssl, tkinter, tkinter.font

# =========================
# Networking (from Ch. 1)
# =========================
class URL:
    def __init__(self, url):
        self.scheme, url = url.split("://", 1)
        assert self.scheme in ["http", "https"]

        if "/" not in url: url += "/"
        self.host, url = url.split("/", 1)
        self.path = "/" + url

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
        statusline = resp.readline()
        version, status, explanation = statusline.split(" ", 2)

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

# =========================
# Tokens (Ch. 3)
# =========================
class Text:
    def __init__(self, text): self.text = text

class Tag:
    def __init__(self, tag): self.tag = tag

def lex(body):
    out, buf, in_tag = [], "", False
    for c in body:
        if c == "<":
            in_tag = True
            if buf: out.append(Text(buf))
            buf = ""
        elif c == ">":
            in_tag = False
            out.append(Tag(buf))
            buf = ""
        else:
            buf += c
    if not in_tag and buf:
        out.append(Text(buf))
    return out

# =========================
# Font cache (Ch. 3)
# =========================
FONTS = {}
def get_font(size, weight, style):
    key = (size, weight, style)
    if key not in FONTS:
        font = tkinter.font.Font(size=size, weight=weight, slant=style)
        label = tkinter.Label(font=font)  # perf helper as in text
        FONTS[key] = (font, label)
    return FONTS[key][0]

# =========================
# Layout (Ch. 2–3)
# =========================
WIDTH, HEIGHT = 800, 600
HSTEP, VSTEP = 13, 18           # margins / paragraph spacing
SCROLL_STEP = 100

class Layout:
    def __init__(self, tokens):
        self.display_list = []       # (x, y, word, font)
        self.cursor_x, self.cursor_y = HSTEP, VSTEP
        self.weight, self.style = "normal", "roman"
        self.size = 12

        self.line = []               # [(x, word, font)]
        for tok in tokens:
            self.token(tok)
        self.flush()                 # flush remaining words

    def token(self, tok):
        if isinstance(tok, Text):
            for word in tok.text.split():
                self.word(word)
        else:
            t = tok.tag
            if t == "i": self.style = "italic"
            elif t == "/i": self.style = "roman"
            elif t == "b": self.weight = "bold"
            elif t == "/b": self.weight = "normal"
            elif t == "small": self.size -= 2
            elif t == "/small": self.size += 2
            elif t == "big": self.size += 4
            elif t == "/big": self.size -= 4
            elif t == "br":
                self.flush()
            elif t == "/p":
                self.flush()
                self.cursor_y += VSTEP

    def word(self, word):
        font = get_font(self.size, self.weight, self.style)
        w = font.measure(word)
        # wrap if needed (ignore trailing space in the check)
        if self.cursor_x + w > WIDTH - HSTEP:
            self.flush()
        # buffer the word; y is computed in flush via baseline
        self.line.append((self.cursor_x, word, font))
        # advance x by word + a space width
        self.cursor_x += w + font.measure(" ")

    def flush(self):
        if not self.line: return
        # Compute tallest ascent/descent among words in the line
        metrics = [font.metrics() for _, _, font in self.line]
        max_ascent = max(m["ascent"] for m in metrics)
        max_descent = max(m["descent"] for m in metrics)
        baseline = self.cursor_y + max_ascent
        # Place each word so its top-left is (x, baseline - ascent)
        for x, word, font in self.line:
            ascent = font.metrics("ascent")
            y = baseline - ascent
            self.display_list.append((x, y, word, font))
        # Advance to next line; 1.25× leading for readability
        self.cursor_y = baseline + int(1.25 * max_descent)
        self.cursor_x = HSTEP
        self.line = []

# =========================
# GUI (Ch. 2–3)
# =========================
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
        tokens = lex(body)
        layout = Layout(tokens)
        self.display_list = layout.display_list
        self.draw()

    def draw(self):
        self.canvas.delete("all")
        for x, y, word, font in self.display_list:
            # cull offscreen rows for speed
            if y > self.scroll + HEIGHT: continue
            if y + font.metrics("linespace") < self.scroll: continue
            self.canvas.create_text(x, y - self.scroll,
                                    text=word, font=font, anchor="nw")

    def scrolldown(self, e):
        self.scroll += SCROLL_STEP
        self.draw()

# =========================
# CLI
# =========================
if __name__ == "__main__":
    import sys
    if len(sys.argv) != 2:
        print("Usage: python3 browser3.py <URL>")
        sys.exit(1)
    Browser().load(URL(sys.argv[1]))
    tkinter.mainloop()
