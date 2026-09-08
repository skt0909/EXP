"""
test_groq_client.py — unit tests for Context_assembler/groq_client.py's
call_groq(). Never hits the real Groq API: requests.post is monkeypatched
in every test.
"""

from unittest.mock import patch

import pytest
import requests

import groq_client
from groq_client import GroqError, call_groq


class _FakeResponse:
    def __init__(self, json_data=None, status_code=200, http_error=None):
        self._json_data = json_data
        self.status_code = status_code
        self._http_error = http_error

    def raise_for_status(self):
        if self._http_error is not None:
            raise self._http_error

    def json(self):
        return self._json_data


@pytest.fixture(autouse=True)
def _fake_api_key(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-test-key")


def test_missing_api_key_raises_groq_error(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    with pytest.raises(GroqError, match="GROQ_API_KEY not set"):
        call_groq("hello")


def test_successful_call_returns_content():
    fake_resp = _FakeResponse(json_data={"choices": [{"message": {"content": "here's my advice"}}]})

    with patch.object(groq_client.requests, "post", return_value=fake_resp) as mock_post:
        result = call_groq("Should I captain Haaland?")

    assert result == "here's my advice"
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["messages"] == [{"role": "user", "content": "Should I captain Haaland?"}]
    assert kwargs["headers"]["Authorization"] == "Bearer fake-test-key"


def test_http_error_status_raises_groq_error():
    http_error = requests.exceptions.HTTPError("401 Client Error")
    fake_resp = _FakeResponse(status_code=401, http_error=http_error)

    with patch.object(groq_client.requests, "post", return_value=fake_resp):
        with pytest.raises(GroqError, match="Groq API call failed"):
            call_groq("hello")


def test_timeout_raises_groq_error():
    with patch.object(groq_client.requests, "post", side_effect=requests.exceptions.Timeout("timed out")):
        with pytest.raises(GroqError, match="Groq API call failed"):
            call_groq("hello", timeout=1)


def test_connection_error_raises_groq_error():
    with patch.object(
        groq_client.requests, "post", side_effect=requests.exceptions.ConnectionError("connection refused")
    ):
        with pytest.raises(GroqError, match="Groq API call failed"):
            call_groq("hello")


def test_malformed_response_missing_choices_raises_groq_error():
    fake_resp = _FakeResponse(json_data={"error": "something went wrong"})

    with patch.object(groq_client.requests, "post", return_value=fake_resp):
        with pytest.raises(GroqError, match="Unexpected Groq response shape"):
            call_groq("hello")


def test_malformed_response_empty_choices_raises_groq_error():
    fake_resp = _FakeResponse(json_data={"choices": []})

    with patch.object(groq_client.requests, "post", return_value=fake_resp):
        with pytest.raises(GroqError, match="Unexpected Groq response shape"):
            call_groq("hello")


def test_custom_model_and_timeout_passed_through():
    fake_resp = _FakeResponse(json_data={"choices": [{"message": {"content": "ok"}}]})

    with patch.object(groq_client.requests, "post", return_value=fake_resp) as mock_post:
        call_groq("hello", model="some-other-model", timeout=5)

    _, kwargs = mock_post.call_args
    assert kwargs["json"]["model"] == "some-other-model"
    assert kwargs["timeout"] == 5
