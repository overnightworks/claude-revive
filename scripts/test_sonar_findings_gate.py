"""What the SonarCloud findings gate reports, driven against a stand-in service.

The stand-in answers on the loopback interface, so every test drives the gate's
real path — the encoded scope, the credential header, the paging, the timeout —
and asserts on what the gate printed and returned. The tests here run on the
runner's own python, with no test dependency (.github/workflows/ci.yml), so the
stand-in and the capture below are what a fixture library would otherwise give.
"""

from __future__ import annotations

import base64
import contextlib
import http.server
import io
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import sonar_findings_gate as gate

PROJECT_KEY = "overnightworks_demo"
STUB_CREDENTIAL = "stub-credential"
DEFAULT_ENVIRONMENT = {
    gate.EVENT_NAME_VARIABLE: "push",
    gate.BRANCH_NAME_VARIABLE: "main",
    gate.CREDENTIAL_VARIABLE: STUB_CREDENTIAL,
}
MEASURES_ENDPOINT = gate.MEASURES_ENDPOINT
ISSUES_ENDPOINT = gate.FINDINGS_ENDPOINT
# A handler that never answers releases at teardown; the bound only keeps a
# failing test from hanging the suite.
UNANSWERED_REQUEST_LIMIT_SECONDS = 30
PATCHED_REQUEST_TIMEOUT_SECONDS = 0.25

Responder = Callable[[http.server.BaseHTTPRequestHandler], None]


@dataclass(frozen=True)
class GateOutcome:
    """What one gate run printed, and how it ended: an exit code or a refusal."""

    printed: str
    reported: str
    exit_code: int | None
    refusal: str

    @property
    def printed_lines(self) -> list[str]:
        return self.printed.splitlines()

    @property
    def reported_lines(self) -> list[str]:
        return self.reported.splitlines()


@dataclass(frozen=True)
class RecordedRequest:
    endpoint: str
    query: dict[str, list[str]]
    raw_query: str
    authorization: str | None


def write_body(
    handler: http.server.BaseHTTPRequestHandler, status: int, body: str, content_type: str
) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def json_body(payload: object) -> Responder:
    def respond(handler: http.server.BaseHTTPRequestHandler) -> None:
        write_body(handler, 200, json.dumps(payload), "application/json")

    return respond


def text_body(text: str, status: int = 200) -> Responder:
    def respond(handler: http.server.BaseHTTPRequestHandler) -> None:
        write_body(handler, status, text, "text/html")

    return respond


def redirect_to(location: str) -> Responder:
    def respond(handler: http.server.BaseHTTPRequestHandler) -> None:
        handler.send_response(302)
        handler.send_header("Location", location)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return respond


def silence(released: threading.Event) -> Responder:
    def respond(_: http.server.BaseHTTPRequestHandler) -> None:
        released.wait(UNANSWERED_REQUEST_LIMIT_SECONDS)

    return respond


class RequestRecordingHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - the name http.server dispatches to
        self.server.stub.answer(self)

    def log_message(self, *args: object) -> None:
        """Keep the stand-in silent; the recorded requests are the evidence."""


@dataclass
class SonarCloudStub:
    """A loopback stand-in for the SonarCloud endpoints the gate reads."""

    responses: dict[str, list[Responder]] = field(default_factory=dict)
    requests: list[RecordedRequest] = field(default_factory=list)
    released: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RequestRecordingHandler)
        self._server.daemon_threads = True
        self._server.stub = self
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def api_root(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/api"

    def answers(self, endpoint: str, *responders: Responder) -> None:
        self.responses[endpoint] = list(responders)

    def never_answers(self, endpoint: str) -> None:
        self.answers(endpoint, silence(self.released))

    def endpoints_requested(self) -> list[str]:
        return [request.endpoint for request in self.requests]

    def requests_to(self, endpoint: str) -> list[RecordedRequest]:
        return [request for request in self.requests if request.endpoint == endpoint]

    def last_request_to(self, endpoint: str) -> RecordedRequest:
        return self.requests_to(endpoint)[-1]

    def answer(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        parsed = urlparse(handler.path)
        endpoint = parsed.path.removeprefix("/api/")
        self.requests.append(
            RecordedRequest(
                endpoint=endpoint,
                query=parse_qs(parsed.query),
                raw_query=parsed.query,
                authorization=handler.headers.get("Authorization"),
            )
        )
        self.next_responder(endpoint)(handler)

    def next_responder(self, endpoint: str) -> Responder:
        queued = self.responses.get(endpoint)
        if not queued:
            return text_body(f"no response queued for {endpoint}", status=500)
        return queued.pop(0) if len(queued) > 1 else queued[0]

    def close(self) -> None:
        self.released.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()


def issue_payload(
    *,
    rule: str = "python:S1481",
    file_path: str = "scripts/example.py",
    line: int | None = 12,
    message: str = "Remove this unused local variable.",
) -> dict[str, object]:
    payload = {"rule": rule, "component": f"{PROJECT_KEY}:{file_path}", "message": message}
    if line is not None:
        payload["line"] = line
    return payload


def issues_page(*issues: dict[str, object], total: int | None = None) -> Responder:
    return json_body({"total": len(issues) if total is None else total, "issues": list(issues)})


def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class GateAgainstAStandIn(unittest.TestCase):
    """The gate runs as CI runs it, against a stand-in that resolves and is clean."""

    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        properties_path = Path(temporary_directory.name) / "sonar-project.properties"
        properties_path.write_text(
            f"sonar.organization=overnightworks\n{gate.PROJECT_KEY_PROPERTY}={PROJECT_KEY}\n",
            encoding="utf-8",
        )
        self.sonar_cloud = SonarCloudStub()
        self.addCleanup(self.sonar_cloud.close)
        self.patch_gate(
            SONAR_PROPERTIES_PATH=properties_path, SONAR_API_ROOT=self.sonar_cloud.api_root
        )
        self.sonar_cloud.answers(MEASURES_ENDPOINT, json_body({"component": {"key": PROJECT_KEY}}))
        self.sonar_cloud.answers(ISSUES_ENDPOINT, issues_page())

    def patch_gate(self, **attributes: object) -> None:
        for name, value in attributes.items():
            patcher = mock.patch.object(gate, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_gate(self, **environment: str) -> GateOutcome:
        """One run of the gate under the environment CI hands it.

        A gate that stops the process ends with a sentence rather than an exit
        code, so the run reports both shapes and every test reads the same way.
        """
        printed, reported = io.StringIO(), io.StringIO()
        exit_code, refusal = None, ""
        with (
            mock.patch.dict(os.environ, {**DEFAULT_ENVIRONMENT, **environment}),
            contextlib.redirect_stdout(printed),
            contextlib.redirect_stderr(reported),
        ):
            try:
                exit_code = gate.main()
            except SystemExit as stopped:
                refusal = str(stopped)
        return GateOutcome(printed.getvalue(), reported.getvalue(), exit_code, refusal)

    def test_a_scope_that_resolves_and_holds_no_findings_reads_as_clean(self) -> None:
        outcome = self.run_gate()

        self.assertEqual(outcome.exit_code, 0, outcome.refusal)
        self.assertEqual(
            outcome.printed_lines,
            [f"SonarCloud clean for '{PROJECT_KEY}' (branch=main): 0 open finding(s)"],
        )
        self.assertEqual(outcome.reported, "")

    def test_it_lists_every_open_finding_and_fails_the_build(self) -> None:
        self.sonar_cloud.answers(
            ISSUES_ENDPOINT,
            issues_page(
                issue_payload(),
                issue_payload(
                    rule="python:S5754",
                    file_path="scripts/other.py",
                    line=None,
                    message="Specify an exception class.",
                ),
            ),
        )

        outcome = self.run_gate()

        self.assertEqual(outcome.exit_code, 1, outcome.refusal)
        self.assertEqual(
            outcome.printed_lines,
            [
                "python:S1481 scripts/example.py:12 Remove this unused local variable.",
                "python:S5754 scripts/other.py:? Specify an exception class.",
            ],
        )
        self.assertEqual(
            outcome.reported_lines, ["2 open SonarCloud finding(s) in scope branch=main"]
        )

    def test_it_reads_every_page_of_a_paginated_result(self) -> None:
        self.sonar_cloud.answers(
            ISSUES_ENDPOINT,
            issues_page(issue_payload(file_path="scripts/first.py"), total=2),
            issues_page(issue_payload(file_path="scripts/second.py"), total=2),
        )

        outcome = self.run_gate()

        self.assertEqual(outcome.exit_code, 1, outcome.refusal)
        self.assertEqual(
            [line.split()[1] for line in outcome.printed_lines],
            ["scripts/first.py:12", "scripts/second.py:12"],
        )
        self.assertEqual(
            [request.query["p"] for request in self.sonar_cloud.requests_to(ISSUES_ENDPOINT)],
            [["1"], ["2"]],
        )

    def test_it_stops_paging_when_a_page_reports_more_findings_than_it_returns(self) -> None:
        self.sonar_cloud.answers(
            ISSUES_ENDPOINT, issues_page(issue_payload(), total=7), issues_page(total=7)
        )

        outcome = self.run_gate()

        self.assertEqual(outcome.exit_code, 1, outcome.refusal)
        self.assertEqual(
            outcome.reported_lines, ["7 open SonarCloud finding(s) in scope branch=main"]
        )

    def test_it_reports_findings_an_answer_lists_while_claiming_a_total_of_zero(self) -> None:
        self.sonar_cloud.answers(ISSUES_ENDPOINT, issues_page(issue_payload(), total=0))

        outcome = self.run_gate()

        self.assertEqual(outcome.exit_code, 1, outcome.refusal)
        self.assertEqual(
            outcome.printed_lines,
            ["python:S1481 scripts/example.py:12 Remove this unused local variable."],
        )
        self.assertEqual(
            outcome.reported_lines, ["1 open SonarCloud finding(s) in scope branch=main"]
        )

    def test_it_stops_when_a_finding_names_no_file_within_the_project(self) -> None:
        self.sonar_cloud.answers(
            ISSUES_ENDPOINT,
            issues_page(
                {"rule": "python:S1481", "component": "no-project-key", "message": "Unused."}
            ),
        )

        outcome = self.run_gate()

        self.assertIn("'no-project-key'", outcome.refusal)
        self.assertNotIn("SonarCloud clean", outcome.printed)

    def test_it_stops_at_the_page_limit_when_the_service_keeps_claiming_more(self) -> None:
        self.patch_gate(MAXIMUM_FINDINGS_PAGES=2)
        self.sonar_cloud.answers(ISSUES_ENDPOINT, issues_page(issue_payload(), total=9_999))

        outcome = self.run_gate()

        self.assertEqual(outcome.exit_code, 1, outcome.refusal)
        self.assertEqual(self.sonar_cloud.endpoints_requested().count(ISSUES_ENDPOINT), 2)
        self.assertEqual(
            outcome.reported_lines, ["9999 open SonarCloud finding(s) in scope branch=main"]
        )

    def test_it_asks_about_the_scope_of_the_run_with_its_value_encoded(self) -> None:
        scopes_and_environments = [
            ("branch=release/2%off & more", {gate.BRANCH_NAME_VARIABLE: "release/2%off & more"}),
            (
                "pullRequest=41",
                {gate.EVENT_NAME_VARIABLE: "pull_request", gate.PULL_REQUEST_NUMBER_VARIABLE: "41"},
            ),
        ]

        for expected_scope, environment in scopes_and_environments:
            with self.subTest(scope=expected_scope):
                parameter_name, _, value = expected_scope.partition("=")

                outcome = self.run_gate(**environment)

                self.assertEqual(outcome.exit_code, 0, outcome.refusal)
                self.assertEqual(
                    outcome.printed_lines,
                    [f"SonarCloud clean for '{PROJECT_KEY}' ({expected_scope}): 0 open finding(s)"],
                )
                for endpoint in (MEASURES_ENDPOINT, ISSUES_ENDPOINT):
                    self.assertEqual(
                        self.sonar_cloud.last_request_to(endpoint).query[parameter_name], [value]
                    )

    def test_it_sends_the_credential_in_a_header_and_never_in_the_query(self) -> None:
        self.run_gate()

        self.assertNotEqual(self.sonar_cloud.requests, [])
        for request in self.sonar_cloud.requests:
            scheme, _, encoded = request.authorization.partition(" ")
            self.assertEqual(scheme, "Basic")
            self.assertEqual(base64.b64decode(encoded).decode(), f"{STUB_CREDENTIAL}:")
            self.assertNotIn(STUB_CREDENTIAL, request.raw_query)

    def test_it_stops_when_the_scope_does_not_resolve(self) -> None:
        self.sonar_cloud.answers(MEASURES_ENDPOINT, text_body("not found", status=404))

        outcome = self.run_gate()

        self.assertIn(
            f"SonarCloud did not resolve {PROJECT_KEY} in scope branch=main (HTTP 404)",
            outcome.refusal,
        )
        self.assertIn("Its findings were never read.", outcome.refusal)
        self.assertEqual(self.sonar_cloud.endpoints_requested(), [MEASURES_ENDPOINT])

    def test_it_stops_before_asking_when_the_credential_is_empty(self) -> None:
        outcome = self.run_gate(**{gate.CREDENTIAL_VARIABLE: ""})

        self.assertIn("SONAR_TOKEN is empty or unset", outcome.refusal)
        self.assertEqual(self.sonar_cloud.requests, [])

    def test_it_stops_when_the_answer_is_not_json(self) -> None:
        self.sonar_cloud.answers(MEASURES_ENDPOINT, text_body("<html>proxy error</html>"))

        outcome = self.run_gate()

        self.assertIn(
            f"SonarCloud answered the {MEASURES_ENDPOINT} request for {PROJECT_KEY} in scope "
            "branch=main with a body that is not JSON.",
            outcome.refusal,
        )

    def test_it_refuses_a_redirect_instead_of_forwarding_the_credential(self) -> None:
        self.sonar_cloud.answers(
            MEASURES_ENDPOINT, redirect_to(f"{self.sonar_cloud.api_root}/elsewhere")
        )

        outcome = self.run_gate()

        self.assertIn("(HTTP 302)", outcome.refusal)
        self.assertEqual(self.sonar_cloud.endpoints_requested(), [MEASURES_ENDPOINT])

    def test_it_stops_when_sonarcloud_accepts_the_call_and_never_answers(self) -> None:
        self.patch_gate(REQUEST_TIMEOUT_SECONDS=PATCHED_REQUEST_TIMEOUT_SECONDS)
        self.sonar_cloud.never_answers(MEASURES_ENDPOINT)

        outcome = self.run_gate()

        self.assertIn(
            f"SonarCloud did not answer within {PATCHED_REQUEST_TIMEOUT_SECONDS}s: the request "
            f"for {PROJECT_KEY} in scope branch=main timed out.",
            outcome.refusal,
        )
        self.assertIn("re-run the job before investigating SonarCloud.", outcome.refusal)

    def test_it_fails_loudly_when_sonarcloud_cannot_be_reached(self) -> None:
        self.patch_gate(SONAR_API_ROOT=f"http://127.0.0.1:{unused_port()}/api")

        with self.assertRaisesRegex(OSError, "Connection refused"):
            self.run_gate()


class TransportFailures(unittest.TestCase):
    """Which failure of the connection the gate reads as having run out of time."""

    def test_it_recognises_which_transport_failures_ran_out_of_time(self) -> None:
        errors_and_expectations = [
            ("read_ran_out_of_time", TimeoutError(), True),
            ("connect_ran_out_of_time", urllib.error.URLError(TimeoutError()), True),
            ("connection_refused", urllib.error.URLError(ConnectionRefusedError()), False),
            ("other_transport_failure", OSError("broken pipe"), False),
        ]

        for failure, error, expected in errors_and_expectations:
            with self.subTest(failure=failure):
                self.assertIs(gate.timed_out(error), expected)


class ProjectKeyFromTheProperties(unittest.TestCase):
    """Where the gate takes the name of the project it asks about."""

    def test_it_reads_the_project_key_of_this_repository(self) -> None:
        self.assertTrue(gate.read_project_key(gate.SONAR_PROPERTIES_PATH))

    def test_it_stops_when_the_properties_file_names_no_project(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        properties_path = Path(temporary_directory.name) / "sonar-project.properties"
        properties_path.write_text("sonar.organization=overnightworks\n", encoding="utf-8")

        with self.assertRaises(SystemExit) as refusal:
            gate.read_project_key(properties_path)

        self.assertIn(f"does not define {gate.PROJECT_KEY_PROPERTY}.", str(refusal.exception))


if __name__ == "__main__":
    unittest.main()
