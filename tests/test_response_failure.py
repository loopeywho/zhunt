"""Pure tests of ``_response_failure``, the truncation/refusal detector shared by v1 and v2.

Carried over verbatim from tests/test_server.py (ResponseFailureTests) so the
coverage survives that file's deletion.
"""

import unittest

from zhunt.router import FailureKind
from zhunt.server import _response_failure


class ResponseFailureTests(unittest.TestCase):
    def test_client_output_cap_makes_length_stop_routine(self) -> None:
        self.assertIsNone(
            _response_failure(
                {"choices": [{"finish_reason": "length"}]},
                request_data={"max_tokens": 1},
            )
        )

    def test_detects_chat_truncation(self) -> None:
        self.assertEqual(
            _response_failure({"choices": [{"finish_reason": "length"}]}),
            FailureKind.TRUNCATION,
        )

    def test_detects_responses_refusal(self) -> None:
        self.assertEqual(
            _response_failure(
                {
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "refusal", "refusal": "No"}],
                        }
                    ]
                }
            ),
            FailureKind.REFUSAL,
        )

    def test_detects_anthropic_stream_truncation(self) -> None:
        self.assertEqual(
            _response_failure(
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "max_tokens"},
                }
            ),
            FailureKind.TRUNCATION,
        )

    def test_detects_responses_stream_truncation(self) -> None:
        self.assertEqual(
            _response_failure(
                {
                    "type": "response.incomplete",
                    "response": {"status": "incomplete"},
                }
            ),
            FailureKind.TRUNCATION,
        )

    def test_normal_response_has_no_failure(self) -> None:
        self.assertIsNone(
            _response_failure({"choices": [{"finish_reason": "stop"}]})
        )


if __name__ == "__main__":
    unittest.main()
