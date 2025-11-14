import socket
import ssl
import sys
import tkinter
import tkinter.font


class URL:
    def __init__(self, url):
        # Split scheme and rest
        self.scheme, rest = url.split("://", 1)
        assert self.scheme in ["http", "https"]

        # Split host[:port] and path
        if "/" in rest:
            host_port, path = rest.split("/", 1)
            self.path = "/" + path
        else:
            host_port = rest
            self.path = "/"

        # Handle optional port
        if ":" in host_port:
            host, port = host_port.split(":", 1)
            self.host = host
            self.port = int(port)
        else:
            self.host = host_port
            self.port = 80 if self.scheme == "http" else 443

    def request(self):
        # Create TCP socket
        s = socket.socket(
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        s.connect((self.host, self.port))

        # Wrap in TLS for HTTPS
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            s = ctx.wrap_socket(s, server_hostname=self.host)

        # Build HTTP/1.0 request
        request = f"GET {self.path} HTTP/1.0\r\nHost: {self.host}\r\n\r\n"
        s.send(request.encode("utf8"))

        # Read response
        response = s.makefile("r", encoding="utf8", newline="\r\n")

        # Status line
        statusline = response.readline()
        version, status, explanation = statusline.split(" ", 2)

        # Headers
        headers = {}
        while True:
            line = response.readline()
            if line == "\r\n":
                break
            header, value = line.split(":", 1)
            headers[header.lower()] = value.strip()

        # No compression or chunking in chapters 1–3
        assert "transfer-encoding" not in headers
        assert "content-encoding" not in headers

        # Body
        body = response.read()
        s.close()
        return body


class Text:
    def __init__(self, text):
        self.text = text


class Tag:
    def __init__(self, tag):
        self.tag = tag


def lex(body):
    """Return a list of Text/Tag tokens from an HTML document."""
    out = []
    buffer = ""
    in_tag = False
    for c in body:
        if c == "<":
            in_tag = True
            if buffer:
                out.append(Text(buffer))
            buffer = ""
        elif c == ">":
            in_tag = False
            out.append(Tag(buffer))
            buffer = ""
        else:
            buffer += c
    if not in_tag and buffer:
        out.append(Text(buffer))
    return out


FONTS = {}


def get_font(size, weight, style):
    key = (size, weight, style)
    if key not in FONTS:
        font = tkinter.font.Font(size=size, weight=weight, slant=style)
        # Label improves metrics performance on some platforms
        label = tkinter.Label(font=font)
        FONTS[key] = (font, label)
    return FONTS[key][0]


WIDTH, HEIGHT = 800, 600
HSTEP, VSTEP = 13, 18


class Layout:
    def __init__(self, tokens):
        self.display_list = []
        self.cursor_x = HSTEP
        self.cursor_y = VSTEP
        self.weight = "normal"
        self.style = "roman"
        self.size = 12
        self.line = []

        for tok in tokens:
            self.token(tok)
        self.flush()

    def token(self, tok):
        if isinstance(tok, Text):
            for word in tok.text.split():
                self.word(word)
        elif tok.tag == "i":
            self.style = "italic"
        elif tok.tag == "/i":
            self.style = "roman"
        elif tok.tag == "b":
            self.weight = "bold"
        elif tok.tag == "/b":
            self.weight = "normal"
        elif tok.tag == "small":
            self.size -= 2
        elif tok.tag == "/small":
            self.size += 2
        elif tok.tag == "big":
            self.size += 4
        elif tok.tag == "/big":
            self.size -= 4
        elif tok.tag == "br":
            self.flush()
        elif tok.tag == "/p":
            self.flush()
            self.cursor_y += VSTEP

    def word(self, word):
        font = get_font(self.size, self.weight, self.style)
        w = font.measure(word)
        if self.cursor_x + w > WIDTH - HSTEP:
            self.flush()
        self.line.append((self.cursor_x, word, font))
        self.cursor_x += w + font.measure(" ")

    def flush(self):
        if not self.line:
            return

        metrics = [font.metrics() for x, word, font in self.line]
        max_ascent = max(metric["ascent"] for metric in metrics)
        max_descent = max(metric["descent"] for metric in metrics)

        baseline = self.cursor_y + 1.25 * max_ascent

        for x, word, font in self.line:
            y = baseline - font.metrics("ascent")
            self.display_list.append((x, y, word, font))

        self.cursor_y = baseline + 1.25 * max_descent
        self.cursor_x = HSTEP
        self.line = []


SCROLL_STEP = 100


class Browser:
    def __init__(self):
        self.window = tkinter.Tk()
        self.canvas = tkinter.Canvas(
            self.window,
            width=WIDTH,
            height=HEIGHT,
        )
        self.canvas.pack()

        self.window.bind("<Down>", self.scrolldown)

        self.scroll = 0
        self.display_list = []

    def draw(self):
        self.canvas.delete("all")
        for x, y, word, font in self.display_list:
            # Skip off-screen lines for speed
            line_height = font.metrics("linespace")
            if y > self.scroll + HEIGHT:
                continue
            if y + line_height < self.scroll:
                continue
            self.canvas.create_text(
                x,
                y - self.scroll,
                text=word,
                font=font,
                anchor="nw",
            )

    def load(self, url):
        body = url.request()
        tokens = lex(body)
        self.display_list = Layout(tokens).display_list
        self.draw()

    def scrolldown(self, e):
        self.scroll += SCROLL_STEP
        self.draw()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python browser.py URL")
        sys.exit(1)

    Browser().load(URL(sys.argv[1]))
    tkinter.mainloop()
