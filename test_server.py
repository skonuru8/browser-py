# test_server.py
import socket, urllib.parse

ENTRIES = ["Pavel was here"]

def form_decode(body):
    params = {}
    for field in (body or "").split("&"):
        if not field: continue
        name, value = field.split("=", 1)
        params[urllib.parse.unquote_plus(name)] = urllib.parse.unquote_plus(value)
    return params

def show_comments():
    out = "<!doctype html>"
    for entry in ENTRIES:
        out += "<p>" + entry + "</p>"
    out += "<form action=/add method=post>"
    out +=   "<p><input name=guest value=Your+name></p>"
    out +=   "<p><button>Sign the book!</button></p>"
    out += "</form>"
    return out

def do_request(method, url, headers, body):
    if method == "GET" and url == "/":
        return "200 OK", show_comments()
    elif method == "POST" and url == "/add":
        params = form_decode(body)
        if "guest" in params: ENTRIES.append(params["guest"])
        return "200 OK", show_comments()
    else:
        return "404 Not Found", "<!doctype html><h1>{} {} not found!</h1>".format(method, url)

def handle_connection(conx):
    req = conx.makefile("b")
    reqline = req.readline().decode("utf8")
    method, url, version = reqline.split(" ", 2)
    headers = {}
    while True:
        line = req.readline().decode("utf8")
        if line == "\r\n": break
        k, v = line.split(":", 1)
        headers[k.casefold()] = v.strip()
    if "content-length" in headers:
        length = int(headers["content-length"])
        body = req.read(length).decode("utf8")
    else:
        body = None
    status, body = do_request(method, url, headers, body)
    resp = "HTTP/1.0 {}\r\nContent-Length: {}\r\n\r\n{}".format(status, len(body.encode("utf8")), body)
    conx.send(resp.encode("utf8"))
    conx.close()

if __name__ == "__main__":
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("", 8000)); s.listen()
    print("Guestbook server on http://localhost:8000/")
    while True:
        conx, _ = s.accept()
        handle_connection(conx)
