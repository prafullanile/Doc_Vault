"""End-to-end smoke test against a running `docker compose up` stack.

Registers a user, uploads a text PDF and a scanned PDF, waits for the worker, and checks the
extracted text. Uses only the standard library, so it runs anywhere Python 3.10+ does:

    python scripts/smoke_test.py            # API at http://localhost:8000
    API=http://host:8000 python scripts/smoke_test.py
"""

import json
import os
import random
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API = os.environ.get("API", "http://localhost:8000")
ROOT = Path(__file__).resolve().parents[1]


def request(method: str, path: str, *, token: str | None = None, body: bytes | None = None,
            content_type: str | None = None) -> tuple[int, dict]:
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if content_type:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(API + path, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed local URL
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def upload(token: str, path: Path) -> str:
    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
        f"filename=\"{path.name}\"\r\nContent-Type: application/pdf\r\n\r\n"
    ).encode() + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    status, data = request(
        "POST", "/v1/documents", token=token, body=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    assert status == 201, (status, data)
    return str(data["document_id"])


def wait_until_ready() -> None:
    for _ in range(60):
        try:
            if request("GET", "/health/ready")[0] == 200:
                return
        except OSError:
            pass
        time.sleep(2)
    sys.exit("API never became ready")


def make_documents(workdir: Path) -> None:
    """Builds the PDFs inside the API container, which also proves PyMuPDF and Pillow work in
    the image, then copies them out."""
    script = (ROOT / "scripts" / "make_smoke_docs.py").read_text()
    subprocess.run(  # noqa: S603, S607
        ["docker", "compose", "exec", "-T", "api", "python", "-"],
        input=script, text=True, check=True, cwd=ROOT,
    )
    for name in ("text.pdf", "scan.pdf"):
        subprocess.run(  # noqa: S603, S607
            ["docker", "compose", "cp", f"api:/tmp/{name}", str(workdir / name)],
            check=True, cwd=ROOT, capture_output=True,
        )


def check(token: str, path: Path, expect_source: str, expect_text: str) -> None:
    doc = upload(token, path)
    print(f"Uploaded {path.name} as {doc}")
    status: dict = {}
    for _ in range(60):
        _, status = request("GET", f"/v1/documents/{doc}/status", token=token)
        if status["status"] in ("PROCESSED", "FAILED"):
            break
        time.sleep(2)
    version = status["current_version"]
    summary = {k: version[k] for k in ("page_count", "ocr_page_count", "language", "error_code")}
    print(f"  {status['status']} {summary}")
    if status["status"] != "PROCESSED":
        sys.exit(f"FAILED: {path.name} ended as {status['status']}")

    _, pages = request("GET", f"/v1/documents/{doc}/pages", token=token)
    first = pages["items"][0]
    if first["source"] != expect_source or expect_text not in first["text"].lower():
        sys.exit(f"FAILED: unexpected page content for {path.name}: {first}")
    print(f"  OK: page 1 is {first['source']}: {first['text'][:60]!r}")


def main() -> None:
    print(f"Waiting for {API} ...")
    wait_until_ready()
    email = f"smoke-{random.randint(0, 10**9)}@example.com"  # noqa: S311
    status, tokens = request(
        "POST", "/v1/auth/register", content_type="application/json",
        body=json.dumps({"email": email, "password": "correct-horse-battery",
                         "organization_name": "Smoke"}).encode(),
    )
    assert status == 201, (status, tokens)
    token = tokens["access_token"]

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        make_documents(workdir)
        check(token, workdir / "text.pdf", "TEXT", "revenue increased")
        check(token, workdir / "scan.pdf", "OCR", "invoice")
    check_search(token)
    check_question(token)
    print("Smoke test passed")


def check_question(token: str) -> None:
    """RAG: with an LLM key the answer must cite the right document; without one, the sources
    must still come back (status LLM_UNAVAILABLE)."""
    status, body = request(
        "POST", "/v1/query", token=token, content_type="application/json",
        body=json.dumps({"question": "By what percentage did revenue increase?"}).encode(),
    )
    assert status == 200, (status, body)
    if not body["sources"] or body["sources"][0]["filename"] != "text.pdf":
        sys.exit(f"FAILED: question retrieved the wrong sources: {body}")
    if body["status"] == "LLM_UNAVAILABLE":
        print("  OK: LLM unavailable (no key, no credits or outage; see `docker compose logs api`)"
              " -> sources still returned")
        return
    cited = [s["filename"] for s in body["sources"] if s["cited"]]
    if body["status"] != "ANSWERED" or "text.pdf" not in cited:
        sys.exit(f"FAILED: answer is not grounded in text.pdf: {body}")
    print(f"  OK: answer ({body['model']}, {body['timings_ms'].get('generation')} ms): "
          f"{body['answer']!r} cites {cited}")


def check_search(token: str) -> None:
    """Real models in the container: the paraphrase shares no keywords with the document."""
    cases = [
        ("keyword", "invoice total", "scan.pdf"),
        ("vector", "how much did sales go up", "text.pdf"),
        ("hybrid", "revenue growth this year", "text.pdf"),
    ]
    for mode, query, expected in cases:
        status, body = request(
            "POST", "/v1/search", token=token, content_type="application/json",
            body=json.dumps({"query": query, "mode": mode}).encode(),
        )
        assert status == 200, (status, body)
        if not body["results"] or body["results"][0]["filename"] != expected:
            sys.exit(f"FAILED: {mode} search for {query!r} did not rank {expected} first: {body}")
        top = body["results"][0]
        print(f"  OK: {mode:7} {query!r} -> {top['filename']} p.{top['page_number']} "
              f"(reranked={body['reranked']}, {body['timings_ms']['total']} ms)")


if __name__ == "__main__":
    main()
