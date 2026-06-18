from __future__ import annotations

import unittest

from gmail.code_extractor import extract_verification_code


class CodeExtractorTests(unittest.TestCase):
    def test_extracts_korean_code_with_brackets(self) -> None:
        text = "보낸사람 : 16006329[NC] 인증번호는 [474913]입니다. 3분 안에 입력해 주세요"
        self.assertEqual(extract_verification_code(text), "474913")

    def test_extracts_korean_code_without_gap_before_brackets(self) -> None:
        text = '보낸사람 : 01055080890(02 김재진)[Web발신][한국모바일인증(주)]본인확인 인증번호[481399]입니다. "타인 노출 금지"'
        self.assertEqual(extract_verification_code(text), "481399")

    def test_extracts_chinese_code(self) -> None:
        text = "您的验证码为 123456，请勿泄露给他人。"
        self.assertEqual(extract_verification_code(text), "123456")

    def test_prefers_keyword_adjacent_code_over_phone_number(self) -> None:
        text = "手机号16006329，本次验证码为 654321，请在3分钟内输入。"
        self.assertEqual(extract_verification_code(text), "654321")

    def test_returns_none_when_missing(self) -> None:
        text = "这封邮件里没有任何验证码，只有说明文本。"
        self.assertIsNone(extract_verification_code(text))


if __name__ == "__main__":
    unittest.main()
