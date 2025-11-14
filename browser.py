import socket
import ssl
import sys


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

        # No compression or chunking in chapter 1
        assert "transfer-encoding" not in headers
        assert "content-encoding" not in headers

        # Body
        body = response.read()
        s.close()
        return body


def show(body):
    in_tag = False
    for c in body:
        if c == "<":
            in_tag = True
        elif c == ">":
            in_tag = False
        elif not in_tag:
            print(c, end="")


def load(url):
    body = URL(url).request()
    show(body)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python browser.py URL")
        sys.exit(1)
    load(sys.argv[1])
