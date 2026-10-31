import unittest

from src.data import (alternatives, availability, effective_price, find_partners, find_product,
                      is_local_city, load_store, needs_special_freight, normalize, search, to_cents)

STORE = load_store()
P = STORE.products


class LoadTest(unittest.TestCase):
    def test_counts(self):
        self.assertEqual((len(P), len(STORE.categories), len(STORE.orders), len(STORE.customers), len(STORE.partners)),
                         (65, 9, 20, 50, 6))
        self.assertEqual(sorted(p.product_id for p in STORE.promotions if p.active), [90, 94, 121, 127])

    def test_money_as_cents(self):
        self.assertEqual((to_cents("599.9"), to_cents("11499"), to_cents("0.1")), (59990, 1149900, 10))
        self.assertEqual(P[81].price_cents, 59990)
        self.assertEqual(STORE.orders[3].total_cents, 345000)

    def test_orders(self):
        self.assertEqual([(i.product_id, i.qty) for i in STORE.orders[15].items], [(82, 1), (106, 1)])
        self.assertIsNone(STORE.orders[9].tracking)
        self.assertIsNone(STORE.orders[9].estimated_delivery)
        self.assertEqual(STORE.orders[4].estimated_delivery.isoformat(), "2025-12-22")

    def test_specs_and_category(self):
        self.assertEqual(P[81].specs["top"], "Spruce")
        self.assertEqual(P[81].category, "Violões")

    def test_active_promo(self):
        self.assertEqual(STORE.active_promo(94).pct, 8)
        self.assertIsNone(STORE.active_promo(93))  # only inactive promos


class NormalizeTest(unittest.TestCase):
    def test_accents_case_digits_plurals(self):
        self.assertEqual(normalize("Violões até R$1000!"), ["violao", "ate", "r", "1000"])
        self.assertEqual(normalize("Takamine GD20"), ["takamine", "gd", "20"])
        self.assertEqual(normalize("Guitarras"), ["guitarra"])
        self.assertEqual(normalize("pedais"), ["pedal"])


class SearchTest(unittest.TestCase):
    def test_typo_and_spacing(self):
        self.assertEqual(find_product(STORE, "takamini gd 20").id, 95)

    def test_plural_category_and_price_cap(self):
        res = search(STORE, text="violões", max_cents=100000)
        self.assertEqual(len(res), 12)
        self.assertTrue(all(p.category_id == 5 for p in res))
        prices = [effective_price(STORE, p) for p in res]
        self.assertEqual(prices, sorted(prices))
        self.assertEqual(res[0].id, 83)
        self.assertNotIn(96, [p.id for p in res])  # stock 0

    def test_unavailable_only_on_request(self):
        self.assertNotIn(96, [p.id for p in search(STORE, text="giannini")])
        self.assertIn(96, [p.id for p in search(STORE, text="giannini", include_unavailable=True)])

    def test_category_filter_excludes_coming_soon(self):
        res = search(STORE, category="Ukuleles")
        self.assertTrue(res and all(p.category_id == 9 for p in res))
        self.assertNotIn(130, [p.id for p in res])

    def test_price_filter_uses_promo_price(self):
        self.assertIn(94, [p.id for p in search(STORE, text="taylor", max_cents=560000)])

    def test_find_product_by_id_and_unknown(self):
        self.assertEqual(find_product(STORE, "95").id, 95)
        self.assertIsNone(find_product(STORE, "stradivarius violino"))

    def test_tie_prefers_closer_name(self):
        self.assertEqual(find_product(STORE, "martin d 28").id, 93)  # not 108 (D-28E)


class StatusTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(availability(P[81]), "disponível")
        self.assertEqual(availability(P[129]), "últimas unidades")
        self.assertEqual(availability(P[96]), "temporariamente indisponível")
        self.assertEqual(availability(P[113]), "fora de linha")
        self.assertEqual(availability(P[130]), "em breve")

    def test_alternatives_nearest_price_same_category(self):
        self.assertEqual([p.id for p in alternatives(STORE, P[96])], [107, 82, 86])

    def test_special_freight(self):
        self.assertTrue(needs_special_freight(P[139]))   # acoustic drum kit
        self.assertFalse(needs_special_freight(P[142]))  # synth, not a digital piano
        self.assertFalse(needs_special_freight(P[95]))

    def test_local_city(self):
        self.assertTrue(is_local_city("  campo GRANDE "))
        self.assertFalse(is_local_city("Dourados"))


class PartnersTest(unittest.TestCase):
    def ids(self, *args, **kw):
        return [p.id for p in find_partners(STORE, *args, **kw)]

    def test_match_and_order(self):
        self.assertEqual(self.ids("cordas de violão"), [1, 6])
        self.assertEqual(self.ids("boquilha"), [5])
        self.assertEqual(self.ids("bateria eletrônica"), [])

    def test_city_filter_and_fallback(self):
        self.assertEqual(self.ids("amplificador", city="Dourados"), [6])
        self.assertEqual(self.ids("pedal", city="Corumbá"), [2, 3, 6])
