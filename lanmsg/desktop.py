import argparse
from urllib.parse import urlparse


def main():
    import webview
    parser = argparse.ArgumentParser(description="Connect to a LANmsg server")
    parser.add_argument("url", nargs="?", default="http://localhost:8000")
    args = parser.parse_args()
    if urlparse(args.url).scheme not in ("http", "https"):
        parser.error("Server address must start with http:// or https://")
    webview.create_window("LANmsg", args.url, width=1100, height=760, min_size=(700, 500))
    webview.start()
