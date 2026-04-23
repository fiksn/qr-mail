import unittest

from core.routing import find_route, parse_allowed_sender_routes


class TestRouting(unittest.TestCase):
    def test_parse_routed_pattern(self) -> None:
        rules = parse_allowed_sender_routes([" *@trusted.com =  foo@bar.com, baz@d.com "])
        self.assertEqual(rules[0].pattern, "*@trusted.com")
        self.assertEqual(rules[0].to_addrs, ["foo@bar.com", "baz@d.com"])

    def test_find_first_match_wins(self) -> None:
        rules = parse_allowed_sender_routes(["*@a.com=a@x.com", "*@a.com=b@x.com"])
        r = find_route("user@a.com", rules)
        self.assertIsNotNone(r)
        self.assertEqual(r.to_addrs, ["a@x.com"])


if __name__ == "__main__":
    unittest.main()
