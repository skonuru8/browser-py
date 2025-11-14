import socket
import ssl
import sys
import tkinter


class URL:
    def __init__(self, url):
        self.scheme, rest = url.split("://", 1)
        assert self.scheme in ["http", "https"]

        if "/" in rest:
            host_port, path = rest.split("/", 1)
            self.path = "/" + path
        else:
            host_port = rest
            self.path = "/"

        if ":" in host_port:
            host, port = host_port.split(":", 1)
            self.host = host
            self.port = int(port)
        else:
            self.host = host_port
            self.port = 80 if self.scheme == "http" else 443

    def request(self):
        s = socket.socket(
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        s.connect((self.host, self.port))

        if self.scheme == "https":
            ctx = ssl.create_default_context()
            s = ctx.wrap_socket(s, server_hostname=self.host)

        request = f"GET {self.path} HTTP/1.0\r\nHost: {self.host}\r\n\r\n"
        s.send(request.encode("utf8"))

        response = s.makefile("r", encoding="utf8", newline="\r\n")

        statusline = response.readline()
        version, status, explanation = statusline.split(" ", 2)

        headers = {}
        while True:
            line = response.readline()
            if line == "\r\n":
                break
            header, value = line.split(":", 1)
            headers[header.lower()] = value.strip()

        assert "transfer-encoding" not in headers
        assert "content-encoding" not in headers

        body = response.read()
        s.close()
        return body


class Browser:
    def __init__(self):
        self.window = tkinter.Tk()
        self.canvas = tkinter.Canvas(self.window, width=800, height=600)
        self.canvas.pack()

    def load(self, url):
        body = URL(url).request()
        self.display(body)

    def display(self, body):
        # Chapter 2: lay out text one word at a time
        cursor_x, cursor_y = 13, 18
        line_height = 18

        for word in body.split():
            word += " "
            w = self.canvas.create_text(cursor_x, cursor_y, text=word, anchor="nw")
            bounds = self.canvas.bbox(w)
            if bounds[2] >= 800:  # word exceeds width
                self.canvas.delete(w)
                cursor_y += line_height
                cursor_x = 13
                w = self.canvas.create_text(cursor_x, cursor_y, text=word, anchor="nw")
                bounds = self.canvas.bbox(w)
            cursor_x = bounds[2]

    def run(self, url):
        self.load(url)
        self.window.mainloop()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python browser.py URL")
        sys.exit(1)

    Browser().run(sys.argv[1])
