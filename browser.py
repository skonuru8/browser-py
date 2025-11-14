import socket
import ssl
import sys
import tkinter


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

        # No compression or chunking in chapter 1/2
        assert "transfer-encoding" not in headers
        assert "content-encoding" not in headers

        # Body
        body = response.read()
        s.close()
        return body


def lex(body):
    """Return text content of an HTML document, stripping tags."""
    text = ""
    in_tag = False
    for c in body:
        if c == "<":
            in_tag = True
        elif c == ">":
            in_tag = False
        elif not in_tag:
            text += c
    return text


WIDTH, HEIGHT = 800, 600
HSTEP, VSTEP = 13, 18
SCROLL_STEP = 100


def layout(text):
    """Compute positions for each character and return a display list."""
    display_list = []
    cursor_x, cursor_y = HSTEP, VSTEP
    for c in text:
        display_list.append((cursor_x, cursor_y, c))
        cursor_x += HSTEP
        if cursor_x >= WIDTH - HSTEP:
            cursor_y += VSTEP
            cursor_x = HSTEP
    return display_list


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
        for x, y, c in self.display_list:
            if y > self.scroll + HEIGHT:
                continue
            if y + VSTEP < self.scroll:
                continue
            self.canvas.create_text(x, y - self.scroll, text=c)

    def load(self, url):
        body = url.request()
        text = lex(body)
        self.display_list = layout(text)
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
