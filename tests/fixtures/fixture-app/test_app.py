import unittest

from app import greeting


class GreetingTest(unittest.TestCase):
    def test_the_greeting_shows_the_submitted_name(self) -> None:
        self.assertEqual(greeting("Kate"), "Hello, Kate")


if __name__ == "__main__":
    unittest.main()
