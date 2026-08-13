"""Browse the bucket in a web browser. Nothing downloads until you open it.

## Why this and not a FUSE mount

rclone cannot authenticate against RunPod's S3 gateway — every request returns
SignatureDoesNotMatch with credentials boto3 accepts, so `rclone mount` is unavailable and
macFUSE would need a kernel extension and a reboot besides. This serves the same browsing
experience over the client that does work.

## Why not a sync

A mirror copies everything up front, including 600 MB of audio you already have locally, and
goes stale the moment the pod writes again. This lists lazily: a directory listing costs one
API call, and an object's bytes move only when you click it. Always live, never a copy.

    python runctl.py browse            # then open http://127.0.0.1:8765
"""
from __future__ import annotations

import html
import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Rendered inline rather than offered as a download.
TEXTUAL = (".json", ".txt", ".log", ".md", ".yml", ".yaml", ".csv", ".py", ".jsonl")

PAGE = """<!doctype html><meta charset="utf-8">
<title>{title}</title>
<style>
 :root {{ --fg:#1c1c1e; --bg:#fff; --dim:#6b6b70; --line:#e3e3e6; --acc:#0a58ca; }}
 @media (prefers-color-scheme:dark) {{
   :root {{ --fg:#e8e8ea; --bg:#161618; --dim:#9a9aa0; --line:#2c2c30; --acc:#7aa7ff; }} }}
 body {{ font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; color:var(--fg);
        background:var(--bg); margin:0; padding:24px 28px; }}
 a {{ color:var(--acc); text-decoration:none; }} a:hover {{ text-decoration:underline; }}
 h1 {{ font-size:15px; font-weight:600; margin:0 0 4px; }}
 .crumb {{ color:var(--dim); margin-bottom:18px; font-size:13px; }}
 table {{ border-collapse:collapse; width:100%; max-width:1100px; }}
 td {{ padding:5px 14px 5px 0; border-bottom:1px solid var(--line); vertical-align:top; }}
 td.sz {{ text-align:right; color:var(--dim); white-space:nowrap; }}
 td.dt {{ color:var(--dim); white-space:nowrap; }}
 pre {{ background:rgba(127,127,127,.09); padding:14px 16px; border-radius:8px;
        overflow-x:auto; max-width:1100px; }}
 .dir::before {{ content:"📁 "; }} .file::before {{ content:"📄 "; }}
</style>
<h1>{title}</h1><div class="crumb">{crumbs}</div>
{body}
"""


def _fmt(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n}"


class Handler(BaseHTTPRequestHandler):
    storage = None
    bucket = ""

    def log_message(self, *a):        # keep the terminal clean
        pass

    def _send(self, body: bytes, ctype="text/html; charset=utf-8", code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        prefix = urllib.parse.unquote(parsed.path.lstrip("/"))
        if "raw" in qs:
            return self._object(prefix, download=True)
        if prefix and not prefix.endswith("/"):
            return self._object(prefix)
        return self._listing(prefix)

    # -- directory ----------------------------------------------------------------------

    def _crumbs(self, prefix: str) -> str:
        parts, acc, out = [p for p in prefix.split("/") if p], "", ['<a href="/">bucket</a>']
        for p in parts:
            acc += p + "/"
            out.append(f'<a href="/{urllib.parse.quote(acc)}">{html.escape(p)}</a>')
        return " / ".join(out)

    def _listing(self, prefix: str):
        cli = self.storage.client
        rows = []
        token, dirs, files = None, [], []
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix, "Delimiter": "/",
                  "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            r = cli.list_objects_v2(**kw)
            dirs += [p["Prefix"] for p in r.get("CommonPrefixes", [])]
            files += [o for o in r.get("Contents", []) if o["Key"] != prefix]
            if not r.get("IsTruncated"):
                break
            token = r.get("NextContinuationToken")

        if prefix:
            up = "/".join(prefix.rstrip("/").split("/")[:-1])
            up = (up + "/") if up else ""
            rows.append(f'<tr><td colspan="3"><a href="/{urllib.parse.quote(up)}">'
                        f'⬆ up</a></td></tr>')
        for d in sorted(dirs):
            name = d[len(prefix):].rstrip("/")
            rows.append(f'<tr><td><a class="dir" href="/{urllib.parse.quote(d)}">'
                        f'{html.escape(name)}/</a></td><td class="sz"></td>'
                        f'<td class="dt"></td></tr>')
        total = 0
        for o in sorted(files, key=lambda x: x["Key"]):
            name = o["Key"][len(prefix):]
            total += o["Size"]
            rows.append(
                f'<tr><td><a class="file" href="/{urllib.parse.quote(o["Key"])}">'
                f'{html.escape(name)}</a></td>'
                f'<td class="sz">{_fmt(o["Size"])}</td>'
                f'<td class="dt">{o["LastModified"]:%Y-%m-%d %H:%M}</td></tr>')

        summary = (f'<p style="color:var(--dim)">{len(dirs)} folders · {len(files)} files '
                   f'· {_fmt(total)}</p>')
        body = summary + "<table>" + "".join(rows) + "</table>"
        if not dirs and not files:
            body = '<p style="color:var(--dim)">(empty)</p>' + body
        page = PAGE.format(title=f"s3://{self.bucket}/{prefix}",
                           crumbs=self._crumbs(prefix), body=body)
        self._send(page.encode())

    # -- object -------------------------------------------------------------------------

    def _object(self, key: str, download: bool = False):
        cli = self.storage.client
        try:
            obj = cli.get_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            return self._send(f"<pre>{html.escape(str(exc)[:400])}</pre>".encode(),
                              code=404)
        data = obj["Body"].read()
        if download or not key.lower().endswith(TEXTUAL):
            return self._send(data, "application/octet-stream")
        text = data.decode("utf-8", "replace")
        if key.endswith(".json"):
            try:
                text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
            except Exception:
                pass
        body = (f'<p><a href="/{urllib.parse.quote(key)}?raw=1">download raw</a> · '
                f'{_fmt(len(data))}</p><pre>{html.escape(text)}</pre>')
        page = PAGE.format(title=key.split("/")[-1], crumbs=self._crumbs(
            key.rsplit("/", 1)[0] + "/" if "/" in key else ""), body=body)
        self._send(page.encode())


def serve(port: int = 8765, open_browser: bool = True) -> None:
    import sys
    sys.path.insert(0, str(REPO))
    from .storage import Storage

    st = Storage()
    if not st.available:
        raise SystemExit("no S3 credentials (runpods3.key)")
    Handler.storage = st
    Handler.bucket = st.require().bucket

    srv = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"\n  browsing s3://{Handler.bucket}  →  {url}")
    print("  listings are live; a file downloads only when you open it")
    print("  Ctrl-C to stop\n")
    if open_browser:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("  stopped")
