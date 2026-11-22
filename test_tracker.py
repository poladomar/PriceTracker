import unittest
from catalog import changes
from pandora_tracker import parse_page, evaluate
from dashboard import render

class Tests(unittest.TestCase):
    def test_parse(self):
        result = parse_page('<script type="application/ld+json">{"name":"Ring","offers":{"price":"19.99","priceCurrency":"EUR"}}</script>')
        self.assertEqual(result["price"], 19.99)

    def test_sale(self):
        p = {"price": 20, "original": 40, "sale": True, "in_stock": True}
        self.assertEqual(changes({"1":{"price":40,"sale":False}}, {"1":p})[0][0], "Sale started")

    def test_history(self):
        state = {}
        evaluate({}, {"price":30,"in_stock":True}, state)
        self.assertTrue(evaluate({}, {"price":20,"in_stock":True}, state))
        self.assertEqual(state["lowest"],20)

    def test_escape(self):
        self.assertIn("&lt;script&gt;", render({"x":{"name":"<script>","last_price":10}}))

if __name__ == "__main__":
    unittest.main()
